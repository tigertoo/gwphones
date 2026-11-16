"""Billing for a tenant's admins: its statements (and their PDFs), this
month so far, and its call history. Admins only, on a router that checks
it itself (as routing.py).

gwphones already answers with prices only (tenant_billing.py); this picks
the same fields again, so nothing new it might add is passed on.
"""
from fastapi import APIRouter, Depends, Request, Response

from . import gw
from .app import Signed, app
from .phone import _audit
from .routing import admin

router = APIRouter(prefix="/api/billing", dependencies=[Depends(admin)])

STATEMENT_KEYS = ("id", "number", "month", "customer", "total", "issued_at")
CALL_KEYS = ("start", "ext", "number", "destination", "seconds", "amount", "state")


def _pick(d: dict, keys) -> dict:
    return {k: d.get(k) for k in keys}


@router.get("/statements")
def api_statements(me: Signed = Depends(admin)):
    r = gw.call(me.user, "GET", "/billing/statements")
    return {"statements": [_pick(s, STATEMENT_KEYS) for s in r["statements"]]}


@router.get("/statements/{sid}/pdf")
def api_statement_pdf(sid: int, request: Request, me: Signed = Depends(admin)):
    r = gw.call(me.user, "GET", f"/billing/statements/{sid}/pdf", raw=True)
    _audit(me, request, "statement downloaded", str(sid))
    return Response(r.content, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": r.headers.get("content-disposition", "inline")})


@router.get("/month")
def api_month(month: str = "", me: Signed = Depends(admin)):
    r = gw.call(me.user, "GET", "/billing/month", params={"month": month})
    pick_lines = lambda lines, keys: [_pick(x, keys) for x in lines]
    return {"month": r["month"], "month_open": r["month_open"], "customers": [{
        "customer": c["customer"],
        "calls": _pick(c["calls"], ("count", "seconds", "amount")),
        "by_destination": pick_lines(c["by_destination"], ("destination", "calls", "seconds", "amount")),
        "numbers": pick_lines(c["numbers"], ("number", "label", "amount")),
        "addons": pick_lines(c["addons"], ("label", "amount")),
        **_pick(c, ("total", "not_priced", "provisional", "billed_already")),
    } for c in r["customers"]]}


@router.get("/calls")
def api_calls(month: str = "", ext: str = "", me: Signed = Depends(admin)):
    r = gw.call(me.user, "GET", "/billing/calls", params={"month": month, "ext": ext})
    return {"month": r["month"], "calls": [_pick(c, CALL_KEYS) for c in r["calls"]], "truncated": r["truncated"]}


app.include_router(router)
