"""The tenant portal's API.

Every route says which session states it accepts (signed_in(...)), and
"full" is the default: a half-signed-in session (password given, code not
yet; or an admin who must set up two-factor codes) reaches only the routes
that say so. Changes are only accepted from the portal's own pages (Origin
check). Runs as one process: lockouts are in the database, but the save
limit and the Status cache (phone.py) are in memory.
"""
import logging

import segno
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import auth, config, db, mail

log = logging.getLogger("gwportal")

app = FastAPI(title="gwportal", openapi_url=None, docs_url=None, redoc_url=None)


@app.exception_handler(auth.Refused)
def _refused(request: Request, e: auth.Refused):
    return JSONResponse({"detail": str(e)}, status_code=e.status)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if not config.ORIGIN or origin != config.ORIGIN:
            return JSONResponse({"detail": "Changes can only be made from the portal itself."}, status_code=403)
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def client_ip(request: Request) -> str:
    # nginx overwrites X-Real-IP with the real address (README); the
    # service itself only listens on 127.0.0.1.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


class Signed:
    """The caller: their session and account."""
    def __init__(self, session, user, token):
        self.session, self.user, self.token = session, user, token


def signed_in(*states):
    """A dependency: a session in one of these states (default: full)."""
    allowed = states or ("full",)

    def dep(request: Request) -> Signed:
        token = request.cookies.get(config.COOKIE, "")
        with db.connect() as conn:
            found = auth.session_for(conn, token)
        if not found:
            raise auth.Refused("Please sign in.", 401)
        session, user = found
        if session["state"] not in allowed:
            raise auth.Refused({"pending_mfa": "Enter the code from your authenticator app first.",
                                "enrol": "Set up two-factor codes first."}.get(session["state"], "Please sign in."),
                               403)
        return Signed(session, user, token)
    return dep


def _cookie(response: Response, token: str):
    response.set_cookie(config.COOKIE, token, httponly=True, secure=True, samesite="strict", path="/")


def _me(user) -> dict:
    return {"email": user["email"], "name": user["name"], "role": user["role"], "tenant": user["tenant_code"],
            "ext": user["ext"], "two_factor": bool(user["totp_secret"])}


# -- signing in ---------------------------------------------------------------------------

class SignIn(BaseModel):
    email: str
    password: str


class Code(BaseModel):
    code: str = ""
    recovery_code: str = ""


@app.get("/api/session")
def api_session(request: Request):
    with db.connect() as conn:
        found = auth.session_for(conn, request.cookies.get(config.COOKIE, ""))
    if not found:
        return {"state": "none"}
    session, user = found
    return {"state": session["state"], **({"me": _me(user)} if session["state"] == "full" else {})}


@app.post("/api/login")
def api_login(req: SignIn, request: Request, response: Response):
    ip = client_ip(request)
    with db.connect() as conn:
        try:
            token, state, user = auth.sign_in(conn, req.email, req.password, ip)
        except auth.Refused:
            db.audit(conn, "sign-in refused", ip=ip, detail=auth.normal_email(req.email)[:120])
            conn.commit()
            raise
        db.audit(conn, "signed in" if state == "full" else f"password right ({state})", user, ip)
    _cookie(response, token)
    return {"state": state, **({"me": _me(user)} if state == "full" else {})}


@app.post("/api/login/code")
def api_login_code(req: Code, request: Request, response: Response,
                   me: Signed = Depends(signed_in("pending_mfa"))):
    ip = client_ip(request)
    with db.connect() as conn:
        try:
            token = auth.give_code(conn, me.session, me.user, req.code, req.recovery_code, ip)
        except auth.Refused:
            db.audit(conn, "wrong two-factor code", me.user, ip)
            conn.commit()
            raise
        db.audit(conn, "signed in" + (" with a recovery code" if req.recovery_code else ""), me.user, ip)
    _cookie(response, token)
    return {"state": "full", "me": _me(me.user)}


@app.post("/api/logout")
def api_logout(request: Request, response: Response):
    with db.connect() as conn:
        auth.end_session(conn, request.cookies.get(config.COOKIE, ""))
    response.delete_cookie(config.COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return {"ok": True}


@app.get("/api/me")
def api_me(me: Signed = Depends(signed_in())):
    return _me(me.user)


# -- two-factor codes ------------------------------------------------------------------------

@app.post("/api/2fa/start")
def api_2fa_start(me: Signed = Depends(signed_in("enrol", "full"))):
    with db.connect() as conn:
        uri = auth.start_totp(conn, me.user)
    return {"uri": uri, "qr": segno.make(uri, error="m").svg_data_uri(scale=5, border=2)}


@app.post("/api/2fa/finish")
def api_2fa_finish(req: Code, request: Request, response: Response,
                   me: Signed = Depends(signed_in("enrol", "full"))):
    ip = client_ip(request)
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (me.user["id"],)).fetchone()
        codes = auth.finish_totp(conn, user, req.code)
        db.audit(conn, "two-factor codes turned on", user, ip)
        if me.session["state"] == "enrol":  # set up: now fully signed in, with a new token
            auth.end_session(conn, me.token)
            _cookie(response, auth.new_session(conn, user, "full", ip))
    return {"recovery_codes": codes}


class Confirm(BaseModel):
    password: str = ""
    code: str = ""


@app.post("/api/2fa/off")
def api_2fa_off(req: Confirm, request: Request, me: Signed = Depends(signed_in())):
    if me.user["role"] == "admin":
        raise auth.Refused("Admins must keep two-factor codes on.", 403)
    with db.connect() as conn:
        if not (auth.check_password(me.user, req.password) and auth.check_code(conn, me.user, req.code)):
            raise auth.Refused("Your password and a current code are needed to turn this off.", 400)
        auth.turn_off_totp(conn, me.user)
        db.audit(conn, "two-factor codes turned off", me.user, client_ip(request))
    return {"ok": True}


@app.post("/api/2fa/recovery-codes")
def api_recovery_codes(req: Code, request: Request, me: Signed = Depends(signed_in())):
    with db.connect() as conn:
        if not auth.check_code(conn, me.user, req.code):
            raise auth.Refused("Enter a current code from your authenticator app.", 400)
        codes = auth.new_recovery_codes(conn, me.user)
        db.audit(conn, "new recovery codes", me.user, client_ip(request))
    return {"recovery_codes": codes}


# -- passwords -----------------------------------------------------------------------------------

class PasswordChange(BaseModel):
    current: str
    new: str


@app.post("/api/password/change")
def api_password_change(req: PasswordChange, request: Request, me: Signed = Depends(signed_in())):
    if not auth.check_password(me.user, req.current):
        raise auth.Refused("Your current password isn't right.", 400)
    auth.check_password_rules(req.new, me.user["email"])
    with db.connect() as conn:
        conn.execute("UPDATE users SET pw_hash = ?, updated_at = ? WHERE id = ?",
                     (auth.hash_password(req.new), db.iso(), me.user["id"]))
        auth.end_all_sessions(conn, me.user["id"], keep_token=me.token)  # signed out everywhere else
        db.audit(conn, "password changed", me.user, client_ip(request))
    return {"ok": True}


class Forgot(BaseModel):
    email: str


FORGOT_REPLY = {"ok": True, "message": "If that address has an account, we've emailed it a link to reset the password."}


@app.post("/api/password/forgot")
def api_forgot(req: Forgot, request: Request):
    ip = client_ip(request)
    with db.connect() as conn:
        if auth.ip_blocked(conn, ip):
            raise auth.Refused("Too many attempts from here. Try again in a while.", 429)
        auth.note_ip_failure(conn, ip)  # each request counts towards the address's limit
        user = conn.execute("SELECT * FROM users WHERE email = ?", (auth.normal_email(req.email),)).fetchone()
        if user and not user["disabled"] and user["pw_hash"]:
            token = auth.new_link(conn, user, "reset")
            db.audit(conn, "reset link asked for", user, ip)
            conn.commit()
            try:
                mail.reset(user, token)
            except mail.NotSent as e:
                log.warning("Reset email not sent: %s", e)
    return FORGOT_REPLY  # the same whether or not the address exists


class LinkPassword(BaseModel):
    token: str
    password: str = ""


@app.post("/api/password/reset")
def api_reset(req: LinkPassword, request: Request):
    ip = client_ip(request)
    with db.connect() as conn:
        if auth.ip_blocked(conn, ip):
            raise auth.Refused("Too many attempts from here. Try again in a while.", 429)
        try:
            user = auth.use_link(conn, req.token, "reset", req.password, ip)
        except auth.Refused:
            conn.commit()
            raise
        db.audit(conn, "password reset", user, ip)
    return {"ok": True}


@app.post("/api/invite/check")
def api_invite_check(req: LinkPassword, request: Request):
    with db.connect() as conn:
        if auth.ip_blocked(conn, client_ip(request)):
            raise auth.Refused("Too many attempts from here. Try again in a while.", 429)
        user = auth.link_user(conn, req.token, "invite")
        if not user:
            auth.note_ip_failure(conn, client_ip(request))
            conn.commit()
            raise auth.Refused("This invitation has expired or was already used. Ask for a new one.", 400)
    return {"email": user["email"], "name": user["name"]}


@app.post("/api/invite/accept")
def api_invite_accept(req: LinkPassword, request: Request):
    ip = client_ip(request)
    with db.connect() as conn:
        if auth.ip_blocked(conn, ip):
            raise auth.Refused("Too many attempts from here. Try again in a while.", 429)
        try:
            user = auth.use_link(conn, req.token, "invite", req.password, ip)
        except auth.Refused:
            conn.commit()
            raise
        db.audit(conn, "invitation accepted", user, ip)
    return {"ok": True}


# -- accounts (used by the invite command and, in part 2, by tenant admins) ------------------------

def create_user(conn, email: str, tenant_code: str, role: str, name: str = "", ext=None, ext_created=None):
    email = auth.normal_email(email)
    if role not in ("admin", "user"):
        raise auth.Refused("Unknown role.", 400)
    if not mail_like(email):
        raise auth.Refused("That doesn't look like an email address.", 400)
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise auth.Refused("That address can't be invited.", 409)  # neutral: it may be another tenant's
    now = db.iso()
    cur = conn.execute("INSERT INTO users (email, name, tenant_code, role, ext, ext_created, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (email, name.strip()[:80], tenant_code, role, ext, ext_created,
                                                            now, now))
    return conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()


def mail_like(email: str) -> bool:
    import re
    return bool(re.fullmatch(r"[^@\s,;<>\"']{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}", email))


# The phone actions and staff management register their routes on app.
from . import phone  # noqa: E402,F401
from . import routing  # noqa: E402,F401
