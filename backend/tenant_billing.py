"""A tenant's own billing, for its portal (admins only there): its issued
statements and their PDFs, this month so far, and its call history.

Prices only, ever. Every answer is built from an allowlist: no cost,
markup, margin, rate, trunk, route, rate sheet or Asterisk detail, and
nothing from "Not assigned" (it mixes every tenant's calls). A tenant's
statements are those of its customers (customers.tenant_id, which never
changes once made); cancelled ones aren't shown.

"This month so far" is the draft a statement would be (statements._snapshot,
the same rounding), per customer, as of the last billing pass (every
minute); calls not priced yet are counted, not priced. Cached for a
minute per tenant and month, since it scans the month's calls.
"""
import json
import time
from decimal import Decimal
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import billing
import statements
from billing import LOCAL_TZ, MONTH_RE, from_micro, get_db
from pbx import current_tenant

router = APIRouter(prefix="/api/pbx/billing")

MAX_CALLS = 2000
CACHE_SECONDS = 60
_cache: Dict[Tuple[int, str], Tuple[float, dict]] = {}


def _month(month: str) -> str:
    from datetime import datetime
    month = month or datetime.now(LOCAL_TZ).strftime("%Y-%m")
    if not MONTH_RE.match(month):
        raise HTTPException(400, "Month must look like 2026-10.")
    return month


def _eur(micro: int) -> str:
    return statements._money2(micro)


def _customers(conn, tenant: int) -> Dict[int, str]:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM customers WHERE tenant_id = ?", (tenant,))}


def _own_statement(conn, sid: int, tenant: int):
    r = conn.execute("SELECT s.* FROM statements s JOIN customers c ON c.id = s.customer_id "
                     "WHERE s.id = ? AND c.tenant_id = ? AND s.cancelled_at IS NULL", (sid, tenant)).fetchone()
    if not r:
        raise HTTPException(404, "Statement not found.")
    return r


@router.get("/statements")
def api_statements():
    tenant = current_tenant()
    with get_db() as conn:
        rows = conn.execute("SELECT s.* FROM statements s JOIN customers c ON c.id = s.customer_id "
                            "WHERE c.tenant_id = ? AND s.cancelled_at IS NULL ORDER BY s.month DESC, s.id DESC",
                            (tenant,)).fetchall()
    out = []
    for r in rows:
        snap = json.loads(r["snapshot"])
        out.append({"id": r["id"], "number": r["number"], "month": r["month"], "customer": snap["customer"]["name"],
                    "total": _eur(r["total_micro"]), "issued_at": r["issued_at"]})
    return {"statements": out}


@router.get("/statements/{sid}/pdf")
def api_statement_pdf(sid: int):
    with get_db() as conn:
        r = _own_statement(conn, sid, current_tenant())
    snap = json.loads(r["snapshot"])
    return Response(statements.statement_pdf(snap), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{statements._filename(snap)}"',
                             "Cache-Control": "no-store"})


def _estimate(tenant: int, month: str) -> dict:
    with get_db() as conn:
        mine = _customers(conn, tenant)
        settings = statements._settings(conn)
        out = []
        for e in billing.month_statement(conn, month):
            cid = e["customer"]["id"]
            if cid not in mine:  # another tenant's, or "Not assigned"
                continue
            unbilled = [c for c in e["calls"] if c["statement_id"] is None]
            priced = [c for c in unbilled if c["status"] in ("rated", "provisional") and c["price_eur"] is not None]
            ddis = [d for d in e["ddis"] if d["statement_id"] is None]
            addons = [a for a in e["addons"] if a["statement_id"] is None]
            snap = statements._snapshot(conn, e, month, settings, "ESTIMATE", priced, ddis, addons)
            t = snap["totals"]
            out.append({
                "customer": mine[cid],
                "calls": {"count": len(priced), "seconds": sum(c["seconds"] for c in snap["calls"]), "amount": _eur(t["calls"])},
                "by_destination": [{"destination": g["destination"], "calls": g["calls"], "seconds": g["seconds"],
                                    "amount": _eur(g["price_micro"])} for g in snap["groups"]],
                "numbers": [{"number": f"+{d['did']}", "label": d["label"], "amount": _eur(d["price_micro"])}
                            for d in snap["ddis"]],
                "addons": [{"label": a["label"], "amount": _eur(a["price_micro"])} for a in snap["addons"]],
                "total": _eur(t["total"]),
                "not_priced": len(unbilled) - len(priced),
                "provisional": sum(c["status"] == "provisional" for c in priced),
                "billed_already": len(e["calls"]) - len(unbilled),
            })
    from datetime import datetime
    return {"month": month, "month_open": month >= datetime.now(LOCAL_TZ).strftime("%Y-%m"), "customers": out}


@router.get("/month")
def api_month(month: str = ""):
    month, tenant = _month(month), current_tenant()
    hit = _cache.get((tenant, month))
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    result = _estimate(tenant, month)
    _cache[(tenant, month)] = (time.monotonic(), result)
    return result


@router.get("/calls")
def api_calls(month: str = "", ext: str = ""):
    """The tenant's outbound calls that connected, newest first, with what
    each costs the tenant: its statement's price, else an estimate (a
    customer's markup on it), else none (not priced yet, or an extension
    no customer has)."""
    month, tenant = _month(month), current_tenant()
    ext = ext.strip()
    with get_db() as conn:
        mine = _customers(conn, tenant)
        prices: Dict[int, Optional[str]] = {}
        for e in billing.month_statement(conn, month):
            if e["customer"]["id"] in mine:
                for c in e["calls"]:
                    prices[c["id"]] = c["price_eur"]
        start, end = billing._month_range(month)
        sql = ("SELECT * FROM calls WHERE tenant_id = ? AND direction = 'out' AND start_utc >= ? AND start_utc < ? "
               "AND status NOT IN ('free', 'failed')")
        args = [tenant, start, end]
        if ext:
            sql, args = sql + " AND ext = ?", args + [ext]
        rows = conn.execute(sql + " ORDER BY start_utc DESC LIMIT ?", args + [MAX_CALLS + 1]).fetchall()
    calls = []
    for r in rows[:MAX_CALLS]:
        line = billing._call_json(r)
        price = prices.get(r["id"])
        calls.append({
            "start": line["start"], "ext": r["ext"] or "",
            "number": f"+{r['number']}" if r["number"] else (r["dialled"] or ""),
            "destination": r["destination"] or "", "seconds": r["billsec"] or 0,
            "amount": None if price is None else from_micro(billing.to_micro(Decimal(price)), 3),
            "state": "billed" if r["statement_id"] is not None and price is not None
            else "estimate" if price is not None else "not priced",
        })
    return {"month": month, "calls": calls, "truncated": len(rows) > MAX_CALLS}
