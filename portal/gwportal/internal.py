"""For gwphones (the operator's console) only: inviting a new tenant's
first admin, and listing a tenant's portal people. Reached only on the
server itself (http://127.0.0.1:8020/internal/...): nginx sends nothing
but /api/ to the portal, so these aren't on the website, and a request
that came through nginx (it sets X-Real-IP) is refused anyway. gwphones
proves itself with the secret the two already share (PORTAL_GW_SECRET
here, GWPHONES_PORTAL_SECRET there)."""
import hmac
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import auth, config, db, gw, mail
from .app import app, create_user

OPERATOR = "the operator (console)"


def _refused(request: Request) -> Optional[JSONResponse]:
    if request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for"):
        return JSONResponse({"detail": "Not found."}, status_code=404)  # came through nginx: not for the website
    given = request.headers.get("x-portal-secret", "")
    if not (config.GW_SECRET and hmac.compare_digest(given.encode(), config.GW_SECRET.encode())):
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return None


def _person(u) -> dict:
    return {"email": u["email"], "name": u["name"], "role": u["role"], "ext": u["ext"],
            "invited": not u["pw_hash"] and u["source"] != "microsoft" and not u["ms_oid"],
            "disabled": bool(u["disabled"]), "microsoft": u["source"] == "microsoft"}


@app.get("/internal/tenants/{code}/people")
def internal_people(code: str, request: Request):
    no = _refused(request)
    if no:
        return no
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users WHERE tenant_code = ? ORDER BY role, email", (code,)).fetchall()
    return {"people": [_person(u) for u in rows], "mail_ready": mail.ready()}


class FirstAdmin(BaseModel):
    email: str
    name: str = ""
    give_link: bool = False  # the invitation link back, to pass on by hand (when email isn't set up)


@app.post("/internal/tenants/{code}/admins")
def internal_invite_admin(code: str, req: FirstAdmin, request: Request):
    """Invites an admin for the tenant, as `gwportal.cli invite` does: the
    tenant must be active; the invitation is emailed (nothing is kept if it
    can't be), or with give_link the link comes back instead."""
    no = _refused(request)
    if no:
        return no
    try:
        tenant = gw.call({"tenant_code": code, "email": OPERATOR}, "GET", "/tenant")
    except auth.Refused as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status)
    if tenant["status"] != "active":
        return JSONResponse({"detail": "The tenant is suspended: reactivate it first."}, status_code=409)
    if not req.give_link and not mail.ready():
        return JSONResponse({"detail": "The portal can't send email (MS_GRAPH_*, EMAIL_FROM_ADDRESS, PORTAL_ORIGIN): "
                                       "ask for the link instead and pass it on yourself."}, status_code=409)
    with db.connect() as conn:
        try:
            user = create_user(conn, req.email, code, "admin", req.name or "")
        except auth.Refused as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status)
        token = auth.new_link(conn, user, "invite")
        db.audit(conn, "first admin invited", None, "", f"{user['email']} for {code} by the operator (console)")
    if req.give_link:
        return {"ok": True, "email": user["email"], "link": mail.link("invite", token)}
    try:
        mail.invite(user, tenant["name"], token)
    except mail.NotSent:
        with db.connect() as conn:
            conn.execute("DELETE FROM links WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        return JSONResponse({"detail": "The invitation couldn't be emailed: nothing was kept. Try again, or ask for "
                                       "the link instead."}, status_code=503)
    return {"ok": True, "email": user["email"]}
