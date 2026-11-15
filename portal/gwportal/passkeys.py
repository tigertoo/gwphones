"""Passkeys (WebAuthn): signing in with a passkey instead of a password
and code, and adding and removing one's own.

- The relying party is PORTAL_ORIGIN's host, never the request's Host;
  the origin checked is PORTAL_ORIGIN.
- A passkey asks for user verification (Face ID, fingerprint or the
  device's PIN) every time, so it counts as both factors: signing in with
  one gives a full session. An admin without two-factor codes set up (the
  operator reset them) still has to set them up first.
- Adding one needs the account's password as well as a signed-in session,
  so a stolen session can't add a passkey that outlasts it.
- Challenges are kept server-side for CHALLENGE_MINUTES, each used once
  (deleted at the first try, right or wrong); a registration challenge is
  bound to the account that asked for it.
- Signing in with a passkey goes through the same gates as a password:
  the address limit, a disabled or locked-out account, failures counted.
- Each user has a random handle (users.webauthn_handle) given to the
  passkey, not their email or id; the handle the passkey sends back must
  match the account its credential belongs to.
- The operator's reset-2fa removes every passkey (a lost phone may hold
  a synced one).
"""
import base64
import json
import secrets
from datetime import timedelta
from typing import Any, Dict
from urllib.parse import urlsplit

from fastapi import Depends, Request, Response
from pydantic import BaseModel
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, AuthenticatorTransport,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from . import auth, clock, config, db
from .app import Signed, _cookie, _me, app, client_ip, signed_in
from .db import iso

CHALLENGE_MINUTES = 5
MAX_PASSKEYS = 10
SIGN_IN_FAILED = "That passkey can't sign in here. Use your email and password instead."


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _rp():
    host = urlsplit(config.ORIGIN).hostname if config.ORIGIN else None
    if not host:
        raise auth.Refused("Passkeys aren't set up on this server yet (PORTAL_ORIGIN).", 503)
    return host


def _handle(conn, user) -> bytes:
    if not user["webauthn_handle"]:
        handle = b64u(secrets.token_bytes(32))
        conn.execute("UPDATE users SET webauthn_handle = ? WHERE id = ? AND webauthn_handle IS NULL", (handle, user["id"]))
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return unb64u(user["webauthn_handle"])


def _new_challenge(conn, kind: str, challenge: bytes, user_id=None) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM webauthn_challenges WHERE expires_at <= ?", (iso(),))
    conn.execute("INSERT INTO webauthn_challenges (token_hash, kind, user_id, challenge, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (auth._h(token), kind, user_id, challenge, iso(clock.now() + timedelta(minutes=CHALLENGE_MINUTES))))
    return token


def _take_challenge(conn, token: str, kind: str, user_id=None):
    """The challenge, deleted whatever happens next; None if it's gone,
    expired, of another kind or another account's."""
    r = conn.execute("SELECT * FROM webauthn_challenges WHERE token_hash = ?", (auth._h(token or ""),)).fetchone()
    if not r:
        return None
    conn.execute("DELETE FROM webauthn_challenges WHERE token_hash = ?", (r["token_hash"],))
    if r["kind"] != kind or r["expires_at"] <= iso() or (kind == "register" and r["user_id"] != user_id):
        return None
    return r["challenge"]


def _transports(value: str):
    out = []
    for t in (value or "").split(","):
        try:
            out.append(AuthenticatorTransport(t))
        except ValueError:
            pass
    return out or None


def delete_all(conn, user_id: int):
    conn.execute("DELETE FROM passkeys WHERE user_id = ?", (user_id,))


# -- adding and removing one's own ----------------------------------------------------------

class RegisterStart(BaseModel):
    password: str


class RegisterFinish(BaseModel):
    challenge_id: str
    credential: Dict[str, Any]
    name: str = ""


@app.get("/api/passkeys")
def api_passkeys(me: Signed = Depends(signed_in())):
    with db.connect() as conn:
        rows = conn.execute("SELECT id, name, created_at, last_used FROM passkeys WHERE user_id = ? ORDER BY id",
                            (me.user["id"],)).fetchall()
    return {"passkeys": [dict(r) for r in rows]}


@app.post("/api/passkeys/register/start")
def api_passkey_register_start(req: RegisterStart, request: Request, me: Signed = Depends(signed_in())):
    rp_id = _rp()
    with db.connect() as conn:
        if not auth.check_password(me.user, req.password):
            auth.note_ip_failure(conn, client_ip(request))
            conn.commit()
            raise auth.Refused("Your password isn't right.", 400)
        existing = conn.execute("SELECT credential_id, transports FROM passkeys WHERE user_id = ?", (me.user["id"],)).fetchall()
        if len(existing) >= MAX_PASSKEYS:
            raise auth.Refused(f"You have {MAX_PASSKEYS} passkeys already: remove one first.", 400)
        options = generate_registration_options(
            rp_id=rp_id, rp_name=config.BRAND, user_id=_handle(conn, me.user), user_name=me.user["email"],
            user_display_name=me.user["name"] or me.user["email"],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=unb64u(r["credential_id"]), transports=_transports(r["transports"]))
                                 for r in existing])
        token = _new_challenge(conn, "register", options.challenge, me.user["id"])
    return {"challenge_id": token, "options": json.loads(options_to_json(options))}


@app.post("/api/passkeys/register/finish")
def api_passkey_register_finish(req: RegisterFinish, request: Request, me: Signed = Depends(signed_in())):
    rp_id = _rp()
    with db.connect() as conn:
        challenge = _take_challenge(conn, req.challenge_id, "register", me.user["id"])
        conn.commit()
        if challenge is None:
            raise auth.Refused("That took too long. Try adding the passkey again.", 400)
        try:
            v = verify_registration_response(credential=req.credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                             expected_origin=config.ORIGIN, require_user_verification=True)
        except InvalidRegistrationResponse:
            raise auth.Refused("That passkey couldn't be added. Try again.", 400)
        cred_id = b64u(v.credential_id)
        if conn.execute("SELECT 1 FROM passkeys WHERE credential_id = ?", (cred_id,)).fetchone():
            raise auth.Refused("That passkey is already added.", 409)
        transports = ",".join(t for t in (req.credential.get("response") or {}).get("transports") or [] if isinstance(t, str))
        name = " ".join((req.name or "").split())[:40] or "Passkey"
        conn.execute("INSERT INTO passkeys (user_id, credential_id, public_key, sign_count, transports, name, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (me.user["id"], cred_id, v.credential_public_key, v.sign_count, transports[:100], name, iso()))
        db.audit(conn, "passkey added", me.user, client_ip(request), name)
    return {"ok": True}


@app.delete("/api/passkeys/{passkey_id}")
def api_passkey_delete(passkey_id: int, request: Request, me: Signed = Depends(signed_in())):
    with db.connect() as conn:
        r = conn.execute("SELECT name FROM passkeys WHERE id = ? AND user_id = ?", (passkey_id, me.user["id"])).fetchone()
        if not r:
            raise auth.Refused("Not found.", 404)
        conn.execute("DELETE FROM passkeys WHERE id = ?", (passkey_id,))
        db.audit(conn, "passkey removed", me.user, client_ip(request), r["name"])
    return {"ok": True}


# -- signing in with one ---------------------------------------------------------------------

class LoginFinish(BaseModel):
    challenge_id: str
    credential: Dict[str, Any]


@app.post("/api/passkey/login/start")
def api_passkey_login_start(request: Request):
    rp_id = _rp()
    with db.connect() as conn:
        if auth.ip_blocked(conn, client_ip(request)):
            raise auth.Refused("Too many attempts from here. Try again in a while.", 429)
        options = generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        token = _new_challenge(conn, "login", options.challenge)
    return {"challenge_id": token, "options": json.loads(options_to_json(options))}


@app.post("/api/passkey/login/finish")
def api_passkey_login_finish(req: LoginFinish, request: Request, response: Response):
    rp_id = _rp()
    ip = client_ip(request)
    with db.connect() as conn:
        if auth.ip_blocked(conn, ip):
            raise auth.Refused("Too many attempts from here. Try again in a while.", 429)
        challenge = _take_challenge(conn, req.challenge_id, "login")
        cred_id = str(req.credential.get("rawId") or req.credential.get("id") or "")
        pk = conn.execute("SELECT * FROM passkeys WHERE credential_id = ?", (cred_id,)).fetchone() if cred_id else None
        user = conn.execute("SELECT * FROM users WHERE id = ?", (pk["user_id"],)).fetchone() if pk else None
        handle = str((req.credential.get("response") or {}).get("userHandle") or "")

        def refuse(why: str, count_account: bool = False):
            auth.note_ip_failure(conn, ip)
            if count_account and user:
                auth._note_account_failure(conn, user)
            db.audit(conn, f"passkey sign-in refused ({why})", user, ip)
            conn.commit()
            raise auth.Refused(SIGN_IN_FAILED)

        if challenge is None:
            refuse("challenge")
        if not user:
            refuse("unknown passkey")
        if user["disabled"] or auth._locked(user):
            refuse("account disabled or locked")
        if handle and handle != (user["webauthn_handle"] or ""):
            refuse("user handle", True)
        try:
            v = verify_authentication_response(credential=req.credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                               expected_origin=config.ORIGIN, credential_public_key=pk["public_key"],
                                               credential_current_sign_count=pk["sign_count"], require_user_verification=True)
        except InvalidAuthenticationResponse:
            refuse("didn't verify", True)
        conn.execute("UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?", (v.new_sign_count, iso(), pk["id"]))
        conn.execute("UPDATE users SET failed = 0, locked_until = NULL WHERE id = ?", (user["id"],))
        state = "enrol" if user["role"] == "admin" and not user["totp_secret"] else "full"
        token = auth.new_session(conn, user, state, ip)
        db.audit(conn, "signed in with a passkey" if state == "full" else "passkey right (enrol)", user, ip, pk["name"])
    _cookie(response, token)
    return {"state": state, **({"me": _me(user)} if state == "full" else {})}
