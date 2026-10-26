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
from pbx import NUMBER_TYPES, PBX_DB_PATH, TRUNKS, VOXBEAM_ROUTES, classify_dialled, lcr_routes

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


def _pbx_settings() -> dict:
    with _read(PBX_DB_PATH) as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def decide(kind: str, number: str) -> dict:
    """The routes to try for `number` (international, no 00) of type `kind`,
    with each candidate's price and why any were dropped."""
    from pbx import DEFAULT_SETTINGS
    s = {**DEFAULT_SETTINGS, **_pbx_settings()}
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
    if mode == "cheapest":
        usable.sort(key=lambda c: (c["typical"], c["per_min"]))
    out["routes"] = usable
    if not usable:
        out["refuse"] = "No route has a price under the cap for this number"
    for c in out["candidates"]:
        for k in ("per_min", "typical"):
            if k in c:
                c[k] = from_micro(c[k], 4)
    return out


def _token(r: dict) -> str:
    return "irishvoip" if r["trunk"] == "irishvoip" else r["route"]


@public_router.get("/route", response_class=PlainTextResponse)
def lcr_route(request: Request, kind: str, num: str):
    """Asked by Asterisk itself only: from this machine, not through nginx,
    with the token from the generated dialplan."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1") or request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for"):
        raise HTTPException(403)
    with _read(PBX_DB_PATH) as conn:
        r = conn.execute("SELECT value FROM settings WHERE key = 'lcr_token'").fetchone()
    if not r or not hmac.compare_digest(request.headers.get("x-lcr-token", ""), r["value"]):
        raise HTTPException(403)
    if kind not in NUMBER_TYPES or not re.fullmatch(r"\d{3,20}", num):
        return PlainTextResponse("REFUSE:bad request")
    started = time.time()
    d = decide(kind, num)
    reply = f"REFUSE:{d['refuse']}" if d.get("refuse") else "OK:" + "/".join(_token(r) for r in d["routes"])
    _last_check["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _recent.appendleft({"at": _last_check["at"], "kind": kind, "number": num, "reply": reply,
                        "routes": [r["label"] for r in d["routes"]], "ms": round((time.time() - started) * 1000)})
    return PlainTextResponse(reply)


@router.get("/route-test")
def api_route_test(number: str):
    """What a call to `number` (as dialled) would do now, with every
    candidate route's price; plus the recent real decisions."""
    kind, national, intl = classify_dialled(number)
    result = None
    if kind == "emergency":
        result = {"kind": kind, "note": "Emergency calls always go straight out via Irish VoIP."}
    elif kind:
        result = decide(kind, intl)
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
