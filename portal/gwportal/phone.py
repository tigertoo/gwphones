"""The phone actions, and tenant admins managing their staff.

A tenant admin manages the whole tenant. A user has one extension, bound
by its identity (number and creation time): every action, list and
recording is checked against it, and if the extension is deleted or
replaced the binding ends (sessions too), so a new 201 is never the old
201's user's. Users may rename their own extension and change its
forwarding, voicemail email (to their own address), PIN and phone setup
(emailed to their own address), and play its recordings; only admins
create or switch off extensions, delete recordings (after a warning) and
manage staff. The tenant's last admin can't be demoted or
disabled.

Saves are limited per tenant (each one reloads Asterisk for the whole
server), and Status is cached for a few seconds per tenant. Both are in
memory: the portal runs as one process.
"""
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import auth, db, gw, mail
from .app import Signed, app, client_ip, create_user, signed_in

SAVES_PER_MINUTE = 20
STATUS_SECONDS = 5
PENDING = {"ok": True, "pending": True, "message": "Saved. It takes effect in a moment."}

_saves: Dict[str, deque] = defaultdict(deque)
_saves_lock = threading.Lock()  # routes run in a thread pool
_status: Dict[str, tuple] = {}
_forwarding: Dict[str, tuple] = {}


def _saving(user):
    """Counts a save against the tenant's limit."""
    with _saves_lock:
        q = _saves[user["tenant_code"]]
        now = time.monotonic()
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= SAVES_PER_MINUTE:
            raise auth.Refused("Too many changes at once. Wait a minute, then try again.", 429)
        q.append(now)


def _cached(cache: Dict[str, tuple], user, path: str) -> dict:
    """gwphones' answer for the tenant, asked at most every STATUS_SECONDS
    (each one runs Asterisk commands)."""
    code = user["tenant_code"]
    hit = cache.get(code)
    if hit and hit[0] > time.monotonic() - STATUS_SECONDS:
        return hit[1]
    data = gw.call(user, "GET", path)
    cache[code] = (time.monotonic(), data)
    return data


def _done(result, extra: Optional[dict] = None) -> dict:
    if isinstance(result, gw.Pending):
        return {**PENDING, **(extra or {})}
    return {"ok": True, **(extra or {})}


def _extensions(user) -> List[dict]:
    return gw.call(user, "GET", "/extensions")["extensions"]


def _bound(me: Signed, request: Request, exts: Optional[List[dict]] = None) -> Optional[str]:
    """A user's extension, still the same one; None for an admin."""
    user = me.user
    if user["role"] == "admin":
        return None
    exts = exts if exts is not None else _extensions(user)
    if user["ext"] and any(e["ext"] == user["ext"] and e["created_at"] == user["ext_created"] for e in exts):
        return user["ext"]
    with db.connect() as conn:
        if user["ext"]:
            conn.execute("UPDATE users SET ext = NULL, ext_created = NULL, updated_at = ? WHERE id = ?",
                         (db.iso(), user["id"]))
            auth.end_all_sessions(conn, user["id"])
            db.audit(conn, "extension binding ended", user, client_ip(request),
                     f"extension {user['ext']} was deleted or replaced")
    raise auth.Refused("Your extension has changed or been removed. Ask your admin to set it up again.", 403)


def _may(me: Signed, request: Request, ext: str) -> Optional[str]:
    """An admin may act on any of the tenant's extensions, a user only on
    their own."""
    mine = _bound(me, request)
    if mine is not None and ext != mine:
        raise auth.Refused("Not found.", 404)
    return mine


def _admin(me: Signed):
    if me.user["role"] != "admin":
        raise auth.Refused("Only your organisation's admins can do that.", 403)


def _audit(me: Signed, request: Request, action: str, detail: str = ""):
    with db.connect() as conn:
        db.audit(conn, action, me.user, client_ip(request), detail)


# -- the tenant and its extensions ----------------------------------------------------------

@app.get("/api/tenant")
def api_tenant(me: Signed = Depends(signed_in())):
    t = gw.call(me.user, "GET", "/tenant")
    return {"code": t["code"], "name": t["name"], "status": t["status"]}


@app.get("/api/extensions")
def api_extensions(request: Request, me: Signed = Depends(signed_in())):
    exts = _extensions(me.user)
    mine = _bound(me, request, exts)
    keep = ("ext", "name", "enabled", "vm_email", "record")
    return {"extensions": [{k: e[k] for k in keep} for e in exts if mine is None or e["ext"] == mine]}


class NewExtension(BaseModel):
    name: str


@app.post("/api/extensions")
def api_new_extension(req: NewExtension, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    _saving(me.user)
    r = gw.call(me.user, "POST", "/extensions", json={"name": req.name})
    ext = r.get("ext") if isinstance(r, dict) else None
    _audit(me, request, "extension created", f"{ext} {req.name}")
    return _done(r, {"ext": ext} if ext else None)


class ExtensionChange(BaseModel):
    name: str
    enabled: Optional[bool] = None


@app.put("/api/extensions/{ext}")
def api_change_extension(ext: str, req: ExtensionChange, request: Request, me: Signed = Depends(signed_in())):
    mine = _may(me, request, ext)
    current = next((e for e in _extensions(me.user) if e["ext"] == ext), None)
    if not current:
        raise auth.Refused("Not found.", 404)
    enabled = current["enabled"] if req.enabled is None else req.enabled
    if mine is not None and enabled != current["enabled"]:
        raise auth.Refused("Only your organisation's admins can switch extensions on or off.", 403)
    _saving(me.user)
    r = gw.call(me.user, "PUT", f"/extensions/{ext}", json={"name": req.name, "enabled": enabled})
    _audit(me, request, "extension changed", f"{ext}: name={req.name}, enabled={enabled}")
    return _done(r)


@app.post("/api/extensions/{ext}/new-password")
def api_new_password(ext: str, request: Request, me: Signed = Depends(signed_in())):
    _may(me, request, ext)
    _saving(me.user)
    r = gw.call(me.user, "POST", f"/extensions/{ext}/reset-password")
    _audit(me, request, "new phone password", ext)
    return _done(r, {"message": "New password made: set the phones up again with a setup link."})


@app.post("/api/extensions/{ext}/new-pin")
def api_new_pin(ext: str, request: Request, me: Signed = Depends(signed_in())):
    _may(me, request, ext)
    _saving(me.user)
    r = gw.call(me.user, "POST", f"/extensions/{ext}/reset-voicemail-pin")
    _audit(me, request, "new voicemail PIN", ext)
    return _done(r, {"message": "New voicemail PIN made: it's in the next setup email."})


class SetupLink(BaseModel):
    app: str = "groundwire"


@app.post("/api/extensions/{ext}/setup-link")
def api_setup_link(ext: str, req: SetupLink, request: Request, me: Signed = Depends(signed_in())):
    _may(me, request, ext)
    r = gw.call(me.user, "POST", f"/extensions/{ext}/setup-link", json={"app": req.app})
    _audit(me, request, "phone setup link made", f"{ext} ({req.app})")
    return {"url": r["url"], "qr": r["qr"], "expires_at": r["expires_at"]}


class SetupEmail(BaseModel):
    apps: List[str]
    to: str = ""


@app.post("/api/extensions/{ext}/setup-email")
def api_setup_email(ext: str, req: SetupEmail, request: Request, me: Signed = Depends(signed_in())):
    mine = _may(me, request, ext)
    # It carries a working setup link and the voicemail PIN: a user's goes
    # to their own address only.
    to = me.user["email"] if mine is not None else req.to.strip()
    _saving(me.user)
    gw.call(me.user, "POST", f"/extensions/{ext}/email-setup", json={"to": to, "apps": req.apps})
    _audit(me, request, "phone setup emailed", f"{ext} to {to}")
    return {"ok": True, "to": to}


class VoicemailEmail(BaseModel):
    email: str = ""


@app.put("/api/extensions/{ext}/voicemail-email")
def api_voicemail_email(ext: str, req: VoicemailEmail, request: Request, me: Signed = Depends(signed_in())):
    mine = _may(me, request, ext)
    email = auth.normal_email(req.email)
    if mine is not None and email not in ("", me.user["email"]):
        raise auth.Refused("Voicemail can only be emailed to your own address.", 403)
    _saving(me.user)
    r = gw.call(me.user, "PUT", f"/extensions/{ext}/voicemail-email", json={"email": email})
    _audit(me, request, "voicemail email changed", f"{ext} to {email or 'nobody'}")
    return _done(r)


# -- forwarding and do not disturb ----------------------------------------------------------------

@app.get("/api/forwarding")
def api_forwarding(request: Request, me: Signed = Depends(signed_in())):
    mine = _bound(me, request)
    data = _cached(_forwarding, me.user, "/forwarding")
    keep = ("dnd", "always", "busy", "no_answer")
    return {"available": data["available"],
            "extensions": {ext: {k: v.get(k) for k in keep} for ext, v in data["extensions"].items()
                           if mine is None or ext == mine}}


class Forwarding(BaseModel):
    dnd: bool = False
    always: str = ""
    busy: str = ""
    no_answer: str = ""


@app.put("/api/extensions/{ext}/forwarding")
def api_set_forwarding(ext: str, req: Forwarding, request: Request, me: Signed = Depends(signed_in())):
    _may(me, request, ext)
    _saving(me.user)
    gw.call(me.user, "PUT", f"/extensions/{ext}/forwarding", json=req.model_dump())
    _forwarding.pop(me.user["tenant_code"], None)  # show the change at once
    _audit(me, request, "forwarding changed", ext)
    return {"ok": True}


# -- status --------------------------------------------------------------------------------------------

@app.get("/api/status")
def api_status(request: Request, me: Signed = Depends(signed_in())):
    mine = _bound(me, request)
    s = _cached(_status, me.user, "/status/tenant")
    return {"running": s["asterisk_running"],
            "phones": {ext: {"status": v.get("status")} for ext, v in s["phones"].items() if mine is None or ext == mine},
            "calls": [{k: c[k] for k in ("ext", "state", "caller_id", "duration_seconds", "recording")}
                      for c in s["calls"] if mine is None or c["ext"] == mine]}


# -- recordings ------------------------------------------------------------------------------------------

def _since_bound(user, name: str) -> bool:
    """A recording made since the user's extension was created: one left
    by an earlier extension with the same number isn't theirs. The file
    name ends with Asterisk's uniqueid, which starts with the call's time
    (seconds since 1970, UTC)."""
    m = _REC_NAME.fullmatch(name)
    if not m or not user["ext_created"]:
        return False
    try:
        return int(m.group(4).split(".")[0]) >= int(datetime.fromisoformat(user["ext_created"]).timestamp())
    except ValueError:
        return False


def _recordings(me: Signed, request: Request, q: str = "") -> List[dict]:
    mine = _bound(me, request)
    data = gw.call(me.user, "GET", "/recordings", params={"ext": mine or "", "q": q})
    return [r for r in data["recordings"]
            if mine is None or (r.get("ext") == mine and _since_bound(me.user, r["name"]))]


# A recording's file name says whose it is: started_ext_other_direction_id.
# (The same pattern as gwphones' REC_NAME_RE.)
_REC_NAME = re.compile(r"[0-9]{8}-[0-9]{6}_([0-9]{3})_([0-9]{0,20}|unknown)_(in|out|int|req)_([0-9.]{1,40})\.ulaw")


def _one_recording(me: Signed, request: Request, name: str):
    """A user may only play or delete their own extension's recordings (an
    admin any of the tenant's: gwphones keeps it to the tenant)."""
    mine = _bound(me, request)
    m = _REC_NAME.fullmatch(name)
    if not m or (mine is not None and (m.group(1) != mine or not _since_bound(me.user, name))):
        raise auth.Refused("Recording not found.", 404)


@app.get("/api/recordings")
def api_recordings(request: Request, q: str = "", me: Signed = Depends(signed_in())):
    keep = ("name", "ext", "other", "direction", "started", "seconds")
    return {"recordings": [{k: r.get(k) for k in keep} for r in _recordings(me, request, q)]}


@app.get("/api/recordings/{name}")
def api_recording(name: str, request: Request, download: bool = False, me: Signed = Depends(signed_in())):
    _one_recording(me, request, name)
    r = gw.call(me.user, "GET", f"/recordings/file/{name}", params={"download": download}, raw=True)
    _audit(me, request, "recording played" if not download else "recording downloaded", name)
    return Response(r.content, media_type="audio/wav", headers={
        "Cache-Control": "no-store", "Content-Disposition": r.headers.get("content-disposition", "inline")})


DELETE_WARNING = ("Deleting a recording can't be undone, and recordings may be needed later (a complaint, a "
                  "dispute, a legal request). Delete it anyway?")


@app.delete("/api/recordings/{name}")
def api_delete_recording(name: str, request: Request, confirm: bool = False, me: Signed = Depends(signed_in())):
    """Admins only, and only once they've confirmed the warning: without
    confirm=true the answer is the warning to show them."""
    if me.user["role"] != "admin":
        raise auth.Refused("Only your organisation's admins can delete recordings.", 403)
    _one_recording(me, request, name)
    if not confirm:
        return JSONResponse({"detail": DELETE_WARNING, "confirm_needed": True}, status_code=409)
    _saving(me.user)
    gw.call(me.user, "DELETE", f"/recordings/file/{name}")
    _audit(me, request, "recording deleted", name)
    return {"ok": True}


# -- staff (tenant admins) -----------------------------------------------------------------------------------

def _user_json(u) -> dict:
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"], "ext": u["ext"],
            "two_factor": bool(u["totp_secret"]), "disabled": bool(u["disabled"]), "invited": not u["pw_hash"]}


def _staff(conn, me: Signed, user_id: int):
    u = conn.execute("SELECT * FROM users WHERE id = ? AND tenant_code = ?", (user_id, me.user["tenant_code"])).fetchone()
    if not u:
        raise auth.Refused("Not found.", 404)
    return u


def _ext_identity(me: Signed, ext: Optional[str]):
    if not ext:
        return None, None
    e = next((e for e in _extensions(me.user) if e["ext"] == ext), None)
    if not e:
        raise auth.Refused(f"There's no extension {ext}.", 400)
    return e["ext"], e["created_at"]


def _active_admins(conn, tenant_code: str, besides: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE tenant_code = ? AND role = 'admin' AND disabled = 0 "
                        "AND id != ?", (tenant_code, besides)).fetchone()[0]


def _send_invite(me: Signed, u):
    tenant = gw.call(me.user, "GET", "/tenant")
    if tenant["status"] != "active":
        raise auth.Refused("This account is suspended: you can't invite anyone.", 403)
    with db.connect() as conn:
        token = auth.new_link(conn, u, "invite")
    try:
        mail.invite(u, tenant["name"], token, me.user["name"] or me.user["email"])
    except mail.NotSent:
        raise auth.Refused("The invitation couldn't be emailed. Try again in a while.", 503)


@app.get("/api/users")
def api_users(me: Signed = Depends(signed_in())):
    _admin(me)
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users WHERE tenant_code = ? ORDER BY email", (me.user["tenant_code"],)).fetchall()
    return {"users": [_user_json(u) for u in rows]}


class NewUser(BaseModel):
    email: str
    name: str = ""
    role: str = "user"
    ext: Optional[str] = None


@app.post("/api/users")
def api_invite(req: NewUser, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    if req.role == "user" and not req.ext:
        raise auth.Refused("Choose the extension this person uses.", 400)
    ext, created = _ext_identity(me, req.ext)
    with db.connect() as conn:
        u = create_user(conn, req.email, me.user["tenant_code"], req.role, req.name, ext, created)
        db.audit(conn, "staff invited", me.user, client_ip(request), f"{u['email']} as {u['role']}")
    try:
        _send_invite(me, u)
    except auth.Refused:
        with db.connect() as conn:  # nothing half-made: they can be invited again
            conn.execute("DELETE FROM users WHERE id = ?", (u["id"],))
        raise
    return {"ok": True, "user": _user_json(u)}


@app.post("/api/users/{user_id}/invite-again")
def api_invite_again(user_id: int, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    with db.connect() as conn:
        u = _staff(conn, me, user_id)
        if u["pw_hash"]:
            raise auth.Refused("They've already accepted. They can reset their password from the sign-in page.", 400)
    _send_invite(me, u)
    _audit(me, request, "invitation sent again", u["email"])
    return {"ok": True}


class UserChange(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    ext: Optional[str] = None
    disabled: Optional[bool] = None


@app.put("/api/users/{user_id}")
def api_change_user(user_id: int, req: UserChange, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    with db.connect() as conn:
        u = _staff(conn, me, user_id)
    role = req.role or u["role"]
    if role not in ("admin", "user"):
        raise auth.Refused("Unknown role.", 400)
    disabled = u["disabled"] if req.disabled is None else int(req.disabled)
    ext, created = (u["ext"], u["ext_created"]) if req.ext is None else _ext_identity(me, req.ext)
    if role == "user" and not ext:
        raise auth.Refused("Choose the extension this person uses.", 400)
    with db.connect() as conn:
        u = _staff(conn, me, user_id)
        was_admin = u["role"] == "admin" and not u["disabled"]
        if was_admin and (role != "admin" or disabled) and _active_admins(conn, u["tenant_code"], u["id"]) == 0:
            raise auth.Refused("Your organisation needs at least one admin.", 400)
        conn.execute("UPDATE users SET name = ?, role = ?, ext = ?, ext_created = ?, disabled = ?, updated_at = ? "
                     "WHERE id = ?", ((req.name if req.name is not None else u["name"]).strip()[:80], role, ext,
                                      created, disabled, db.iso(), u["id"]))
        if role != u["role"] or disabled != u["disabled"] or ext != u["ext"]:
            auth.end_all_sessions(conn, u["id"], keep_token=me.token if u["id"] == me.user["id"] else "")
        db.audit(conn, "staff changed", me.user, client_ip(request),
                 f"{u['email']}: role={role}, ext={ext}, disabled={bool(disabled)}")
    return {"ok": True}
