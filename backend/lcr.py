"""
Route checks and least-cost routing for outbound calls.

Every outbound call (all but 112/999) asks this, from the generated
dialplan, before dialling: GET /lcr/route?kind=<number type>&num=<number,
international without 00>, with the X-LCR-Token header. The answer is
plain text, no newline:

  OK:irishvoip/0011103     routes to try, in order (trunk, or a Voxbeam
                           route prefix); the next only if one can't take it
  REFUSE:<reason>          don't dial

Which routes are candidates depends on the number type's setting in the
Trunks section: the trunk order, one trunk, or "cheapest" (every ticked
route, cheapest first). Then, whatever the setting, a route is dropped if
its rate sheet has no price for the number or its price is over the cap
(€ ex VAT per minute, per number type or the default); none left means
REFUSE. "Cheapest" compares the cost of a typical 2-minute call, so
connection fees and billing increments count. A route whose rate sheet
has expired keeps its latest prices (decided 2026-10-26); the Trunks
section warns about it, since the cap can't see a newer price until the
new sheet is uploaded.

If this doesn't answer (the dashboard is restarting or down), the dialplan
uses the normal trunk order for Irish numbers and refuses international
calls (decided 2026-10-26: the cap can't be checked then, and
international is where the costly mistakes are).

The check must be quick and change nothing: it reads the databases with
a 1 s timeout, never takes billing's lock, and uses only the newest
exchange rate already fetched (billing keeps it fresh).
"""

import hmac
import re
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

import billing
from billing import bill_seconds, from_micro, latest_fx, rate_for, route_label, to_micro
from pbx import (NUMBER_TYPES, PBX_DB_PATH, TRUNKS, VOXBEAM_ROUTES, changes_log, classify_dialled, cli_lists,
                 current_tenant, e164, lcr_routes)

TYPICAL_SECONDS = 120  # "cheapest" compares the cost of a call this long
public_router = APIRouter(prefix="/lcr")
router = APIRouter(prefix="/api/pbx")

_recent: deque = deque(maxlen=50)  # the last decisions, for the Trunks section
_last_check = {"at": None}
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def _pbx_settings(tenant: Optional[int] = None) -> dict:
    """As pbx.get_settings: the server's settings, then the tenant's own
    (price caps among them) on top; the main tenant's by default."""
    from pbx import MAIN_TENANT
    with _read(PBX_DB_PATH) as conn:
        s = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        try:
            s.update({r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM tenant_settings WHERE tenant_id = ?", (MAIN_TENANT if tenant is None else tenant,))})
        except sqlite3.OperationalError:
            pass  # not migrated yet: still all in settings
        return s


def _tenant_by_code(code: str) -> Optional[int]:
    """A call's tenant from its account code: none is the main tenant's,
    a code that's no tenant's is None."""
    from pbx import MAIN_TENANT
    if not code:
        return MAIN_TENANT
    with _read(PBX_DB_PATH) as conn:
        r = conn.execute("SELECT id FROM tenants WHERE code = ?", (code,)).fetchone()
    return r["id"] if r else None


def _shows(lists: dict, c: dict, cli: str) -> bool:
    codes = lists.get(f"{c['trunk']}{c['route']}")
    return not codes or any(cli.startswith(code) for code in codes)


def decide(kind: str, number: str, tenant: Optional[int] = None, cli: str = "") -> dict:
    """The routes to try for `number` (international, no 00) of type `kind`,
    with each candidate's price (against the tenant's caps) and why any
    were dropped. cli: the caller ID the call would show; routes that
    can't show it are dropped, and if none can, the tenant's main number is
    tried instead (out["cli"] then says which to show)."""
    from pbx import DEFAULT_SETTINGS
    s = {**DEFAULT_SETTINGS, **_pbx_settings(tenant)}
    mode = s.get(f"route_{kind}", "default")
    cap_text = s.get(f"cap_{kind}") or s.get("cap_default") or ""
    cap = to_micro(Decimal(cap_text)) if cap_text else None
    out = {"kind": kind, "number": number, "mode": mode, "cap": cap_text, "candidates": [], "routes": []}
    if mode == "off":
        out["refuse"] = "These numbers are blocked"
        return out
    candidates = lcr_routes(s, mode)  # [(trunk, route), ...] in the setting's order
    if not candidates:
        out["refuse"] = "No route is enabled for these numbers"
        return out
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _read(billing.BILLING_DB) as conn:
        for trunk, route in candidates:
            c = {"trunk": trunk, "route": route, "label": route_label(trunk, route)}
            out["candidates"].append(c)
            sheet, row, rate, reason = rate_for(conn, trunk, route, number, at)
            if not row:
                c["dropped"] = reason
                continue
            fx = latest_fx(conn, row["currency"])
            if not fx:
                c["dropped"] = f"No {row['currency']}→EUR exchange rate fetched yet"
                continue
            secs = bill_seconds(TYPICAL_SECONDS, row["conn_unit"], row["bill_unit"])
            typical = (Decimal(rate) * secs / 60).quantize(Decimal(1), rounding=ROUND_CEILING) + row["conn_fee_micro"]
            c["per_min"] = int(Decimal(rate) * fx[0])
            c["typical"] = int(typical * fx[0])
            c.update(destination=row["destination"], prefix=row["prefix"],
                     increments=f"{row['conn_unit']}/{row['bill_unit']}", conn_fee=from_micro(row["conn_fee_micro"], 4),
                     currency=row["currency"], expired=bool(row["expires_at"] and at >= row["expires_at"]))
            if cap is not None and c["per_min"] > cap:
                c["dropped"] = f"€{from_micro(c['per_min'], 4)}/min is over the €{cap_text} cap"
    usable = [c for c in out["candidates"] if "dropped" not in c]
    lists = cli_lists(s)
    shown = e164(cli)
    if lists and shown and usable:
        can = [c for c in usable if _shows(lists, c, shown)]
        if not can:
            main = e164(s.get("outbound_cid", ""))
            fallback = [c for c in usable if _shows(lists, c, main)] if main and main != shown else []
            if fallback:
                out["cli"] = "+" + main
                out["cli_note"] = f"No route can show +{shown}: the main number +{main} is shown instead"
                can = fallback
            else:
                out["refuse"] = f"No route can show the caller ID +{shown}" + (f" or the main number +{main}" if main else "")
        for c in usable:
            if c not in can:
                c["dropped"] = f"Can't show caller ID +{shown}"
        usable = can
    if mode == "cheapest":
        usable.sort(key=lambda c: (c["typical"], c["per_min"]))
    out["routes"] = usable
    if not usable and "refuse" not in out:
        out["refuse"] = "No route has a price under the cap for this number"
    for c in out["candidates"]:
        for k in ("per_min", "typical"):
            if k in c:
                c[k] = from_micro(c[k], 4)
    return out


def _token(r: dict) -> str:
    return "irishvoip" if r["trunk"] == "irishvoip" else r["route"]


@public_router.get("/route", response_class=PlainTextResponse)
def lcr_route(request: Request, kind: str, num: str, tenant: str = "", cli: str = ""):
    """Asked by Asterisk itself only: from this machine, not through nginx,
    with the token from the generated dialplan."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1") or request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for"):
        raise HTTPException(403)
    with _read(PBX_DB_PATH) as conn:
        r = conn.execute("SELECT value FROM settings WHERE key = 'lcr_token'").fetchone()
    if not r or not hmac.compare_digest(request.headers.get("x-lcr-token", ""), r["value"]):
        raise HTTPException(403)
    if kind not in NUMBER_TYPES or not re.fullmatch(r"\d{3,20}", num) or not re.fullmatch(r"\d{0,4}", tenant):
        return PlainTextResponse("REFUSE:bad request")
    if cli and not re.fullmatch(r"\+?[0-9]{6,20}", cli):
        # Unreadable: never refuse a call for it, just don't filter by it.
        changes_log.info(f"Route check: caller ID {cli[:30]!r} isn't a number, so no caller-ID rule applied")
        cli = ""
    tid = _tenant_by_code(tenant)  # the caller's account code: empty for the main tenant
    if tid is None:
        return PlainTextResponse("REFUSE:unknown tenant")
    started = time.time()
    d = decide(kind, num, tid, cli)
    reply = f"REFUSE:{d['refuse']}" if d.get("refuse") else "OK:" + "/".join(_token(r) for r in d["routes"])
    if not d.get("refuse") and d.get("cli"):
        reply += f"|CLI:{d['cli']}"  # the dialplan shows this number instead
        changes_log.info(f"Call to +{num} (tenant {tenant or '1001'}): {d['cli_note']}")
    elif d.get("refuse", "").startswith("No route can show"):
        changes_log.info(f"Call to +{num} (tenant {tenant or '1001'}) refused: {d['refuse']}")
    _last_check["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _recent.appendleft({"at": _last_check["at"], "kind": kind, "number": num, "reply": reply, "cli": cli,
                        "note": d.get("cli_note", ""),
                        "routes": [r["label"] for r in d["routes"]], "ms": round((time.time() - started) * 1000)})
    return PlainTextResponse(reply)


@router.get("/route-test")
def api_route_test(number: str, cli: str = ""):
    """What a call to `number` (as dialled) would do now, with every
    candidate route's price; plus the recent real decisions."""
    kind, national, intl = classify_dialled(number)
    result = None
    if kind == "emergency":
        result = {"kind": kind, "note": "Emergency calls always go straight out via Irish VoIP."}
    elif kind:
        # as a call showing cli would go, or the tenant's main number
        main = _pbx_settings(current_tenant()).get("outbound_cid", "")
        result = decide(kind, intl, current_tenant(), cli or main)
        result["national"] = national
    return {"result": result, "recognised": bool(kind), "recent": list(_recent),
            "last_check": _last_check["at"], "started_at": STARTED_AT,
            "last_outbound_call": _last_outbound(), "expired_sheets": _expired_sheets()}


def _expired_sheets() -> List[dict]:
    """Routes whose newest rate sheet has expired. Their latest prices are
    still used (decided 2026-10-26), so the cap only knows about a price
    rise once the newer sheet is uploaded: this is shown as a warning."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with _read(billing.BILLING_DB) as conn:
            rows = conn.execute("SELECT trunk, route, filename, valid_to FROM rate_sheets s WHERE id = "
                                "(SELECT id FROM rate_sheets WHERE trunk = s.trunk AND route = s.route "
                                "ORDER BY uploaded_at DESC, id DESC LIMIT 1)").fetchall()
    except sqlite3.Error:
        return []
    return [{"label": route_label(r["trunk"], r["route"]), "filename": r["filename"], "expired": r["valid_to"]}
            for r in rows if r["valid_to"] and r["valid_to"] < now]


def _last_outbound() -> Optional[str]:
    try:
        with _read(billing.BILLING_DB) as conn:
            r = conn.execute("SELECT max(start_utc) AS t FROM calls WHERE direction = 'out'").fetchone()
        return r["t"] if r else None
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# Coverage: which destinations a number type can call right now
#
# The same rules as decide(), applied to every prefix in the candidate
# routes' current rate sheets at once (in memory, not one query per
# prefix), grouped by the sheets' destination names.
# ---------------------------------------------------------------------------

IRISH_MOBILE = ("35383", "35385", "35386", "35387", "35389")
IRISH_FREEPHONE = ("3531800",)


def _probes(kind: str, prefixes: set) -> set:
    """The prefixes to check for a number type. Irish ranges are checked
    as themselves too (35387...), since a sheet may only have a broader
    prefix (3538, or just 353) that covers them."""
    if kind == "international":
        return {p for p in prefixes if not p.startswith("353") and not "353".startswith(p)}
    if kind == "landline":
        return {p for p in prefixes if p.startswith("353") and not p.startswith(IRISH_MOBILE + IRISH_FREEPHONE)}
    bases = IRISH_MOBILE if kind == "mobile" else IRISH_FREEPHONE
    return {p for p in prefixes if p.startswith(bases)} | {b for b in bases if any(b.startswith(p) for p in prefixes)}
_coverage_cache: dict = {}


def _route_rows(conn, trunk: str, route: str, at: str) -> dict:
    """prefix -> (destination, per-minute micro-units, currency, conn, bill,
    fee) from the sheet in force at `at`, as rate_for() would price them."""
    sheet = billing._sheet_for(conn, trunk, route, at)
    if not sheet:
        return {}
    out = {}
    for r in conn.execute("SELECT * FROM rates WHERE sheet_id = ?", (sheet["id"],)):
        rate = r["rate_micro"]
        if r["change_at"] and at < r["change_at"] and r["rate_before_micro"] is not None:
            rate = r["rate_before_micro"]
        out[r["prefix"]] = (r["destination"] or "", rate, r["currency"], r["conn_unit"], r["bill_unit"], r["conn_fee_micro"])
    return out


def coverage(kind: str, tenant: Optional[int] = None) -> dict:
    from pbx import DEFAULT_SETTINGS
    s = {**DEFAULT_SETTINGS, **_pbx_settings(tenant)}
    mode = s.get(f"route_{kind}", "default")
    cap_text = s.get(f"cap_{kind}") or s.get("cap_default") or ""
    cap = to_micro(Decimal(cap_text)) if cap_text else None
    candidates = lcr_routes(s, mode) if mode != "off" else []
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _read(billing.BILLING_DB) as conn:
        sheets = conn.execute("SELECT max(id) AS m, count(*) AS n FROM rate_sheets").fetchone()
        fx = {r["currency"]: latest_fx(conn, r["currency"])
              for r in conn.execute("SELECT DISTINCT currency FROM rates").fetchall()}
        key = (kind, mode, cap_text, tuple(candidates), sheets["m"], sheets["n"], tuple(sorted((k, str(v)) for k, v in fx.items())))
        if key in _coverage_cache:
            return _coverage_cache[key]
        tables = [(t, r, route_label(t, r), _route_rows(conn, t, r, at)) for t, r in candidates]
    prefixes = sorted(_probes(kind, {p for *_, rows in tables for p in rows}))
    groups = {}
    for p in prefixes:
        priced, reasons, name, best = [], [], "", 0
        for t, r, label, rows in tables:
            match = next((p[:i] for i in range(len(p), 0, -1) if p[:i] in rows), None)
            if not match:
                continue
            dest, rate, cur, cu, bu, fee = rows[match]
            if len(match) > best:  # name it after the most precise match
                name, best = dest, len(match)
            if not fx.get(cur):
                continue
            per_min = int(Decimal(rate) * fx[cur][0])
            secs = bill_seconds(TYPICAL_SECONDS, cu, bu)
            typical = int(((Decimal(rate) * secs / 60).quantize(Decimal(1), rounding=ROUND_CEILING) + fee) * fx[cur][0])
            if cap is not None and per_min > cap:
                reasons.append((per_min, label))
                continue
            priced.append((typical, per_min, label, dest))
        if mode == "cheapest":
            priced.sort()
        g = groups.setdefault(name or f"+{p}", {"destination": name or f"+{p}", "prefixes": [], "blocked": [],
                                                "routes": {}, "min": None, "max": None, "over": None, "unpriced": 0})
        if priced:
            _, per_min, label, dest = priced[0]
            g["prefixes"].append((p, label, per_min))
            g["routes"][label] = g["routes"].get(label, 0) + 1
            g["min"] = per_min if g["min"] is None else min(g["min"], per_min)
            g["max"] = per_min if g["max"] is None else max(g["max"], per_min)
        else:
            g["blocked"].append(p)
            if reasons:  # keep the cheapest price that was over the cap
                g["over"] = min(g["over"] or reasons[0], min(reasons))
            else:
                g["unpriced"] += 1
    rows = []
    for g in groups.values():
        n_ok, n_bad = len(g["prefixes"]), len(g["blocked"])
        rows.append({
            "destination": g["destination"],
            "status": "available" if not n_bad else "unavailable" if not n_ok else "partly",
            "available": n_ok, "blocked": n_bad,
            "sample": ([x[0] for x in g["prefixes"]] + g["blocked"])[:3],
            "routes": sorted(g["routes"], key=lambda k: -g["routes"][k]),
            "per_min": None if g["min"] is None else (from_micro(g["min"], 4) if g["min"] == g["max"]
                                                       else f"{from_micro(g['min'], 4)}–{from_micro(g['max'], 4)}"),
            "why": ([f"cheapest is €{from_micro(g['over'][0], 4)}/min on {g['over'][1]}, over the €{cap_text} cap"]
                    if g["over"] else []) + ([f"{g['unpriced']} prefix(es) have no price on an allowed route"]
                                             if g["unpriced"] else []),
            "_prefixes": g["prefixes"], "_blocked": g["blocked"],
        })
    rows.sort(key=lambda r: r["destination"].lower())
    result = {"kind": kind, "mode": mode, "cap": cap_text, "routes": [label for *_, label, _rows in tables],
              "rows": rows, "totals": {st: sum(r["status"] == st for r in rows) for st in ("available", "partly", "unavailable")},
              "prefixes": len(prefixes)}
    _coverage_cache.clear()  # keep only the latest
    _coverage_cache[key] = result
    return result


@router.get("/coverage")
def api_coverage(kind: str = "international"):
    if kind not in NUMBER_TYPES:
        raise HTTPException(400, "Unknown number type.")
    result = coverage(kind, current_tenant())
    # Every prefix goes along for the prefix filter: about 0.5 MB for 54,000.
    return {**result, "rows": [{**{k: v for k, v in r.items() if not k.startswith("_")},
                                "all_prefixes": [x[0] for x in r["_prefixes"]] + r["_blocked"]}
                               for r in result["rows"]]}


@router.get("/coverage.csv")
def api_coverage_csv(kind: str = "international"):
    import csv
    import io
    from fastapi.responses import Response
    if kind not in NUMBER_TYPES:
        raise HTTPException(400, "Unknown number type.")
    result = coverage(kind, current_tenant())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["destination", "prefix", "callable", "route", "eur_per_min", "reason"])
    for r in result["rows"]:
        for p, label, per_min in r["_prefixes"]:
            w.writerow([r["destination"], p, "yes", label, from_micro(per_min, 4), ""])
        for p in r["_blocked"]:
            w.writerow([r["destination"], p, "no", "", "", "; ".join(r["why"])])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="coverage-{kind}.csv"'})
