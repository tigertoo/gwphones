"""Microsoft 365 (Entra ID): signing in with a work account, and a tenant's
staff kept in step with a group in its Microsoft 365.

One app registration of ours (PORTAL_MS_CLIENT_ID / PORTAL_MS_CLIENT_SECRET,
multi-tenant) does both: delegated openid/profile/email to sign people in,
and application User.Read.All and GroupMember.Read.All, which each
organisation's Microsoft 365 admin consents to once, to read its group.

Linking: a tenant admin signs in with Microsoft from Staff, and the
Microsoft organisation (tid) in that id_token, from our own code exchange,
is the one linked: never a value from a redirect, which anyone could
forge. One Microsoft organisation per tenant, audited.

Signing in: a person is found only by (tid, oid) from the id_token: never
by email, which a Microsoft admin sets and nobody verifies. Microsoft
replaces the password only: an admin still gives the portal's second
factor. The id_token comes straight from Microsoft's token endpoint over
TLS, on the confidential code flow with PKCE, so its claims are checked
(iss, aud, exp, nonce, tid) without fetching signing keys (OpenID Connect
Core 3.1.3.7).

Sync (every SYNC_MINUTES, or Sync now): the group's members become the
tenant's staff. Admins are those also in the admins group (if one is set);
everyone else needs an extension, read from Exchange's custom attribute N
(extensionAttributeN), which must be one of the tenant's extensions, one
person each. Leavers (out of the group, or their account disabled) are
disabled, and enabled again if they come back. The last active admin is
never disabled or demoted. Local people (invited by email) are left alone,
except one with the same email in this tenant, who is linked. Nothing
secret (tokens, the client secret) is logged or returned.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import timedelta
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi import Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import auth, clock, config, db, gw
from .app import Signed, _cookie, app, client_ip, signed_in
from .db import iso

log = logging.getLogger("gwportal")

CLIENT_ID = os.environ.get("PORTAL_MS_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("PORTAL_MS_CLIENT_SECRET", "").strip()
LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
FLOW_COOKIE = "__Host-gwportal-ms"  # Lax: it must come back on Microsoft's redirect
FLOW_MINUTES = 10
SYNC_MINUTES = 15
TIMEOUT = 15
SKEW = 300  # seconds of clock difference allowed on exp

# Replaced in tests: (method, url, data/params, headers) -> httpx.Response-like
_send = None


def ready() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and config.ORIGIN)


def _http(method: str, url: str, *, data=None, params=None, headers=None):
    if _send:
        return _send(method, url, data=data, params=params, headers=headers)
    with httpx.Client(timeout=TIMEOUT) as c:
        return c.request(method, url, data=data, params=params, headers=headers)


class MsError(Exception):
    """A plain message for the person, never Microsoft's own text."""


# -- the sign-in flow --------------------------------------------------------------------------------------

def _redirect_uri() -> str:
    return f"{config.ORIGIN}/api/microsoft/callback"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _start(conn, purpose: str, user_id: Optional[int] = None):
    """(the URL to send the browser to, the flow cookie's token). State,
    nonce and the PKCE verifier stay here, single-use, for FLOW_MINUTES."""
    token, state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
    conn.execute("DELETE FROM ms_flows WHERE expires_at <= ?", (iso(),))
    conn.execute("INSERT INTO ms_flows (token_hash, purpose, user_id, state, nonce, verifier, expires_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (auth._h(token), purpose, user_id, state, nonce, verifier,
                                                  iso(clock.now() + timedelta(minutes=FLOW_MINUTES))))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    url = f"{LOGIN}/organizations/oauth2/v2.0/authorize?" + urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": _redirect_uri(), "response_mode": "query",
        "scope": "openid profile email", "state": state, "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256", "prompt": "select_account"})
    return url, token


def _take_flow(conn, token: str, state: str):
    r = conn.execute("SELECT * FROM ms_flows WHERE token_hash = ?", (auth._h(token or ""),)).fetchone()
    if not r:
        return None
    conn.execute("DELETE FROM ms_flows WHERE token_hash = ?", (r["token_hash"],))
    if r["expires_at"] <= iso() or not secrets.compare_digest(r["state"], state or ""):
        return None
    return r


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise MsError("Microsoft's answer couldn't be read.")


def _exchange(code: str, verifier: str, nonce: str) -> dict:
    """The verified claims of the signed-in person: tid, oid, name."""
    try:
        r = _http("POST", f"{LOGIN}/organizations/oauth2/v2.0/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "authorization_code", "code": code,
            "redirect_uri": _redirect_uri(), "code_verifier": verifier, "scope": "openid profile email"})
        data = r.json()
    except (httpx.HTTPError, ValueError):
        raise MsError("Microsoft couldn't be reached. Try again in a minute.")
    if r.status_code != 200 or "id_token" not in data:
        log.warning("Microsoft sign-in: token exchange refused (%s)", str(data.get("error", r.status_code))[:60])
        raise MsError("Microsoft didn't confirm the sign-in. Try again.")
    c = _claims(data["id_token"])
    tid, oid = str(c.get("tid", "")), str(c.get("oid", ""))
    if (not tid or not oid or c.get("aud") != CLIENT_ID or c.get("iss") != f"{LOGIN}/{tid}/v2.0"
            or not isinstance(c.get("exp"), (int, float)) or c["exp"] + SKEW < time.time()
            or not secrets.compare_digest(str(c.get("nonce", "")), nonce)):
        raise MsError("Microsoft's answer didn't check out. Try again.")
    return {"tid": tid, "oid": oid, "name": str(c.get("name", ""))[:80]}


@app.get("/api/microsoft/available")
def api_available():
    return {"available": ready()}


@app.post("/api/microsoft/signin/start")
def api_signin_start(request: Request):
    if not ready():
        raise auth.Refused("Signing in with Microsoft isn't set up.", 404)
    with db.connect() as conn:
        if auth.ip_blocked(conn, client_ip(request)):
            raise auth.Refused("Too many attempts from here. Try again in a while.", 429)
        url, token = _start(conn, "signin")
    return _flow_answer(url, token)


@app.post("/api/microsoft/link/start")
def api_link_start(me: Signed = Depends(signed_in())):
    _admin(me)
    if not ready():
        raise auth.Refused("Microsoft 365 isn't set up on this portal yet: ask your provider.", 404)
    with db.connect() as conn:
        url, token = _start(conn, "link", me.user["id"])
    return _flow_answer(url, token)


def _flow_answer(url: str, token: str):
    r = JSONResponse({"url": url})
    r.set_cookie(FLOW_COOKIE, token, httponly=True, secure=True, samesite="lax", path="/",
                 max_age=FLOW_MINUTES * 60)
    return r


def _back(query: str) -> RedirectResponse:
    r = RedirectResponse(f"/?microsoft={query}", status_code=303)
    r.delete_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return r


@app.get("/api/microsoft/callback")
def api_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Microsoft's redirect back. The portal's own session cookie is
    SameSite=Strict, so it isn't sent here: who started a link comes from
    the flow (made in a signed-in request), found by its Lax cookie."""
    ip = client_ip(request)
    with db.connect() as conn:
        if auth.ip_blocked(conn, ip):
            return _back("busy")
        flow = _take_flow(conn, request.cookies.get(FLOW_COOKIE, ""), state)
        conn.commit()
        if not flow:
            auth.note_ip_failure(conn, ip)
            return _back("expired")
        if error or not code:
            return _back("cancelled")
        try:
            who = _exchange(code, flow["verifier"], flow["nonce"])
        except MsError as e:
            auth.note_ip_failure(conn, ip)
            db.audit(conn, f"Microsoft sign-in refused ({e})", ip=ip)
            return _back("failed")
        if flow["purpose"] == "link":
            return _finish_link(conn, flow, who, ip)
        return _finish_signin(conn, who, ip)


def _finish_link(conn, flow, who: dict, ip: str):
    admin = conn.execute("SELECT * FROM users WHERE id = ?", (flow["user_id"],)).fetchone()
    if not admin or admin["disabled"] or admin["role"] != "admin":
        return _back("failed")
    taken = conn.execute("SELECT tenant_code FROM ms_links WHERE ms_tenant = ?", (who["tid"],)).fetchone()
    if taken and taken["tenant_code"] != admin["tenant_code"]:
        db.audit(conn, "Microsoft 365 link refused (linked elsewhere)", admin, ip, who["tid"])
        return _back("elsewhere")
    other = conn.execute("SELECT id FROM users WHERE ms_tenant = ? AND ms_oid = ? AND id != ?",
                         (who["tid"], who["oid"], admin["id"])).fetchone()
    if other:
        return _back("account_taken")
    mine = conn.execute("SELECT ms_tenant FROM ms_links WHERE tenant_code = ?", (admin["tenant_code"],)).fetchone()
    if mine and mine["ms_tenant"] != who["tid"]:
        return _back("other_org")  # unlink first
    if not mine:
        conn.execute("INSERT INTO ms_links (tenant_code, ms_tenant, linked_by, linked_at) VALUES (?, ?, ?, ?)",
                     (admin["tenant_code"], who["tid"], admin["email"], iso()))
    # The admin just proved both accounts: they can sign in with Microsoft from now on.
    conn.execute("UPDATE users SET ms_tenant = ?, ms_oid = ?, updated_at = ? WHERE id = ?",
                 (who["tid"], who["oid"], iso(), admin["id"]))
    db.audit(conn, "Microsoft 365 linked", admin, ip, f"organisation {who['tid']}")
    return _back("linked")


def _finish_signin(conn, who: dict, ip: str):
    link = conn.execute("SELECT * FROM ms_links WHERE ms_tenant = ?", (who["tid"],)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE ms_tenant = ? AND ms_oid = ?",
                        (who["tid"], who["oid"])).fetchone() if link else None
    if not user or user["tenant_code"] != link["tenant_code"] or user["disabled"] or auth._locked(user):
        auth.note_ip_failure(conn, ip)
        db.audit(conn, "Microsoft sign-in refused (no account here)", user, ip)
        return _back("no_account")
    state = "pending_mfa" if user["totp_secret"] else ("enrol" if user["role"] == "admin" else "full")
    token = auth.new_session(conn, user, state, ip)
    db.audit(conn, "signed in with Microsoft" if state == "full" else f"Microsoft sign-in right ({state})", user, ip)
    r = _back("signed_in")
    _cookie(r, token)
    return r


# -- the organisation's settings (admins, in Staff) ---------------------------------------------------------

def _admin(me: Signed):
    if me.user["role"] != "admin":
        raise auth.Refused("Only your organisation's admins can do that.", 403)


def _link(conn, tenant_code: str):
    return conn.execute("SELECT * FROM ms_links WHERE tenant_code = ?", (tenant_code,)).fetchone()


def consent_url(ms_tenant: str = "organizations") -> str:
    """Microsoft's admin consent page: for the linked organisation, or
    before linking for whichever organisation its admin signs in to (the
    app is unverified, so a non-admin can't consent while connecting)."""
    return f"{LOGIN}/{ms_tenant}/v2.0/adminconsent?" + urlencode({
        "client_id": CLIENT_ID, "scope": "https://graph.microsoft.com/.default",
        "redirect_uri": f"{config.ORIGIN}/api/microsoft/consented", "state": "consent"})


@app.get("/api/microsoft/consented")
def api_consented():
    """Back from the Microsoft 365 admin's consent. Nothing here is
    trusted: the next sync or preview finds out whether it worked."""
    return RedirectResponse("/?microsoft=consented", status_code=303)


def _link_json(link) -> dict:
    if not link:
        return {"linked": False, "consent_url": consent_url() if ready() else ""}
    return {"linked": True, "organisation": link["ms_tenant"], "group": link["group_name"],
            "admin_group": link["admin_group_name"], "attribute": link["attribute"],
            "last_sync_at": link["last_sync_at"], "last_result": json.loads(link["last_result"] or "null"),
            "consent_url": consent_url(link["ms_tenant"]), "linked_by": link["linked_by"]}


@app.get("/api/microsoft")
def api_settings(me: Signed = Depends(signed_in())):
    _admin(me)
    with db.connect() as conn:
        return {"available": ready(), **_link_json(_link(conn, me.user["tenant_code"]))}


class Settings(BaseModel):
    group: str = ""          # a group's name or object id
    admin_group: str = ""    # optional: its members are admins
    attribute: int = 1       # Exchange custom attribute 1-15: the extension


@app.put("/api/microsoft")
def api_save_settings(req: Settings, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    if not 1 <= req.attribute <= 15:
        raise auth.Refused("Choose a custom attribute from 1 to 15.", 400)
    with db.connect() as conn:
        link = _link(conn, me.user["tenant_code"])
    if not link:
        raise auth.Refused("Connect Microsoft 365 first.", 400)
    try:
        g = _find_group(link["ms_tenant"], req.group) if req.group.strip() else None
        a = _find_group(link["ms_tenant"], req.admin_group) if req.admin_group.strip() else None
    except MsError as e:
        raise auth.Refused(str(e), 400)
    if not g:
        raise auth.Refused("Choose the group whose members use the phone system.", 400)
    with db.connect() as conn:
        conn.execute("UPDATE ms_links SET group_id = ?, group_name = ?, admin_group_id = ?, admin_group_name = ?, "
                     "attribute = ? WHERE tenant_code = ?", (g["id"], g["name"], a["id"] if a else "",
                                                            a["name"] if a else "", req.attribute,
                                                            me.user["tenant_code"]))
        db.audit(conn, "Microsoft 365 sync settings", me.user, client_ip(request),
                 f"group {g['name']}, admins {a['name'] if a else 'none'}, custom attribute {req.attribute}")
    return {"ok": True}


@app.delete("/api/microsoft")
def api_unlink(request: Request, me: Signed = Depends(signed_in())):
    """Unlinks: nobody signs in with Microsoft any more and sync stops.
    Synced people are disabled (they have no password); an admin can
    remove them, or link again."""
    _admin(me)
    with db.connect() as conn:
        code = me.user["tenant_code"]
        others = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_code = ? AND role = 'admin' AND disabled = 0 "
                              "AND pw_hash IS NOT NULL", (code,)).fetchone()[0]
        if not others:
            raise auth.Refused("Your organisation would have no admin left who can sign in with a password: "
                               "set one up first (Account), or invite one.", 400)
        # Without a password, synced people can't sign in any more: disabled.
        for u in conn.execute("SELECT id FROM users WHERE tenant_code = ? AND source = 'microsoft' AND pw_hash IS NULL",
                              (code,)).fetchall():
            conn.execute("UPDATE users SET disabled = 1, disabled_by = 'admin', updated_at = ? WHERE id = ?",
                         (iso(), u["id"]))
            auth.end_all_sessions(conn, u["id"])
        conn.execute("UPDATE users SET ms_tenant = NULL, ms_oid = NULL, source = 'local' WHERE tenant_code = ? "
                     "AND pw_hash IS NOT NULL", (code,))
        conn.execute("DELETE FROM ms_links WHERE tenant_code = ?", (code,))
        db.audit(conn, "Microsoft 365 unlinked", me.user, client_ip(request))
    return {"ok": True}


@app.post("/api/microsoft/preview")
def api_preview(me: Signed = Depends(signed_in())):
    _admin(me)
    return _run(me.user["tenant_code"], apply=False)


@app.post("/api/microsoft/sync")
def api_sync(request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    result = _run(me.user["tenant_code"], apply=True)
    with db.connect() as conn:
        db.audit(conn, "Microsoft 365 synced", me.user, client_ip(request), _summary(result))
    return result


# -- Microsoft Graph -----------------------------------------------------------------------------------------

_tokens: Dict[str, tuple] = {}  # ms_tenant -> (token, expires): in memory only


def _app_token(ms_tenant: str) -> str:
    cached = _tokens.get(ms_tenant)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    try:
        r = _http("POST", f"{LOGIN}/{ms_tenant}/oauth2/v2.0/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default"})
        data = r.json()
    except (httpx.HTTPError, ValueError):
        raise MsError("Microsoft couldn't be reached. Try again in a minute.")
    if r.status_code != 200 or "access_token" not in data:
        codes = " ".join(str(c) for c in data.get("error_codes", []))
        log.warning("Microsoft app token refused for an organisation (%s %s)", data.get("error", r.status_code), codes)
        if any(c in codes for c in ("65001", "7000229", "500011")) or data.get("error") == "invalid_client":
            raise MsError("Your Microsoft 365 admin needs to approve the phone portal first (Approve in Microsoft 365).")
        raise MsError("Microsoft refused the request. Try again later.")
    _tokens[ms_tenant] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def _graph(ms_tenant: str, url: str, params=None) -> dict:
    for attempt in range(3):
        try:
            r = _http("GET", url, params=params, headers={"Authorization": f"Bearer {_app_token(ms_tenant)}",
                                                          "ConsistencyLevel": "eventual"})
        except httpx.HTTPError:
            raise MsError("Microsoft couldn't be reached. Try again in a minute.")
        if r.status_code == 429 and attempt < 2:
            time.sleep(min(int(r.headers.get("Retry-After", "2") or 2), 10))
            continue
        if r.status_code == 403:
            raise MsError("Microsoft 365 hasn't allowed the phone portal to read your groups: ask your Microsoft 365 "
                          "admin to approve it (Approve in Microsoft 365).")
        if r.status_code == 404:
            raise MsError("That group wasn't found in your Microsoft 365.")
        if r.status_code != 200:
            raise MsError("Microsoft refused the request. Try again later.")
        return r.json()
    raise MsError("Microsoft is busy. Try again in a minute.")


def _find_group(ms_tenant: str, name_or_id: str) -> dict:
    value = name_or_id.strip()
    if len(value) == 36 and value.count("-") == 4:
        g = _graph(ms_tenant, f"{GRAPH}/groups/{value}", {"$select": "id,displayName"})
        return {"id": g["id"], "name": g.get("displayName", "")}
    escaped = value.replace("'", "''")
    found = _graph(ms_tenant, f"{GRAPH}/groups", {"$filter": f"displayName eq '{escaped}'",
                                                  "$select": "id,displayName"}).get("value", [])
    if not found:
        raise MsError(f"There's no group called “{value}” in your Microsoft 365.")
    if len(found) > 1:
        raise MsError(f"More than one group is called “{value}”: give its object id instead.")
    return {"id": found[0]["id"], "name": found[0].get("displayName", "")}


def _members(ms_tenant: str, group_id: str, attribute: int) -> List[dict]:
    url = f"{GRAPH}/groups/{group_id}/transitiveMembers/microsoft.graph.user"
    params = {"$select": "id,displayName,mail,userPrincipalName,accountEnabled,userType,onPremisesExtensionAttributes",
              "$top": "999"}
    out = []
    while url:
        data = _graph(ms_tenant, url, params)
        for u in data.get("value", []):
            attrs = u.get("onPremisesExtensionAttributes") or {}
            out.append({"oid": u["id"], "name": (u.get("displayName") or "")[:80],
                        "email": auth.normal_email(u.get("mail") or u.get("userPrincipalName") or ""),
                        "enabled": bool(u.get("accountEnabled")), "guest": u.get("userType") == "Guest",
                        "ext": str(attrs.get(f"extensionAttribute{attribute}") or "").strip()})
        url, params = data.get("@odata.nextLink"), None
    return out


# -- sync ----------------------------------------------------------------------------------------------------

def _summary(result: dict) -> str:
    if result.get("error"):
        return result["error"][:200]
    c = result["counts"]
    return (f"{c['added']} added, {c['changed']} changed, {c['disabled']} disabled, {c['enabled']} enabled, "
            f"{len(result['problems'])} problems")


def _run(tenant_code: str, apply: bool) -> dict:
    with db.connect() as conn:
        link = _link(conn, tenant_code)
    if not link:
        raise auth.Refused("Connect Microsoft 365 first.", 400)
    if not link["group_id"]:
        raise auth.Refused("Choose the group to sync first.", 400)
    caller = {"tenant_code": tenant_code, "email": "microsoft-365-sync"}
    try:
        members = _members(link["ms_tenant"], link["group_id"], link["attribute"])
        admins = ({m["oid"] for m in _members(link["ms_tenant"], link["admin_group_id"], link["attribute"])}
                  if link["admin_group_id"] else set())
        exts = {e["ext"]: e["created_at"] for e in gw.call(caller, "GET", "/extensions")["extensions"]}
    except (MsError, auth.Refused) as e:
        result = {"error": str(e), "at": iso()}
        if apply:
            with db.connect() as conn:
                conn.execute("UPDATE ms_links SET last_sync_at = ?, last_result = ? WHERE tenant_code = ?",
                             (result["at"], json.dumps(result), tenant_code))
        return result
    with db.connect() as conn:
        result = plan(conn, link, members, admins, exts)
        if apply:
            _apply(conn, link, result)
            conn.execute("UPDATE ms_links SET last_sync_at = ?, last_result = ? WHERE tenant_code = ?",
                         (result["at"], json.dumps(result), tenant_code))
    return result


def plan(conn, link, members: List[dict], admins: set, exts: Dict[str, str]) -> dict:
    """What a sync does: actions (add, link, change, enable, disable) and
    the problems that stop someone being synced. Writes nothing."""
    tenant, tid, attr = link["tenant_code"], link["ms_tenant"], link["attribute"]
    problems, actions = [], []
    present = [m for m in members if m["enabled"]]

    # Who should be here, as what. Staff need an extension of the tenant's own, one person each.
    candidates = []
    for m in present:
        r = "admin" if m["oid"] in admins else "user"
        if m.get("guest"):
            # their id in the group isn't the one their own organisation's sign-in gives
            problems.append({"who": m["email"] or m["name"], "problem": "a guest from another organisation: not synced"})
        elif not m["email"]:
            problems.append({"who": m["name"], "problem": "no email address"})
        elif r == "user" and not m["ext"]:
            problems.append({"who": m["email"], "problem": f"custom attribute {attr} (their extension) is empty"})
        elif r == "user" and m["ext"] not in exts:
            problems.append({"who": m["email"], "problem": f"there's no extension {m['ext']}"})
        else:
            candidates.append({**m, "role": r, "ext": m["ext"] if r == "user" else None,
                               "ext_created": exts.get(m["ext"]) if r == "user" else None})
    per_ext: Dict[str, list] = {}
    for c in candidates:
        if c["ext"]:
            per_ext.setdefault(c["ext"], []).append(c["email"])
    shared = {e for e, who in per_ext.items() if len(who) > 1}
    for e in sorted(shared):
        problems.append({"who": ", ".join(per_ext[e]), "problem": f"they share extension {e}: one person each"})
    wanted = {c["oid"]: c for c in candidates if c["ext"] not in shared}

    for oid, w in wanted.items():
        u = conn.execute("SELECT * FROM users WHERE ms_tenant = ? AND ms_oid = ?", (tid, oid)).fetchone()
        if not u:
            # Never matched by email: only a local person of this tenant with
            # the same address is linked, and only here, by sync.
            same = conn.execute("SELECT * FROM users WHERE email = ?", (w["email"],)).fetchone()
            if same and same["tenant_code"] != tenant:
                problems.append({"who": w["email"], "problem": "this address is used in another organisation's portal"})
            elif same and same["ms_oid"]:
                problems.append({"who": w["email"], "problem": "already linked to another Microsoft account"})
            elif same and same["disabled"]:
                problems.append({"who": w["email"], "problem": "disabled by an admin here: not linked or enabled"})
            elif same:
                actions.append({"do": "link", "id": same["id"], **_fields(w)})
            else:
                actions.append({"do": "add", **_fields(w)})
            continue
        changes = []
        if w["name"] and w["name"] != u["name"]:
            changes.append("name")
        if w["role"] != u["role"]:
            changes.append("role")
        if (w["ext"] or None) != (u["ext"] or None) or (w["ext"] and w["ext_created"] != u["ext_created"]):
            changes.append("extension")
        if u["source"] != "microsoft":  # e.g. the admin who linked: in the group, so sync manages them now
            changes.append("managed by Microsoft 365")
        if u["disabled"] and u["disabled_by"] != "sync":
            # An admin disabled them in Staff (say, a compromised account): sync never undoes that.
            problems.append({"who": w["email"], "problem": "disabled by an admin here: not enabled again"})
        elif u["disabled"]:
            actions.append({"do": "enable", "id": u["id"], **_fields(w)})
        elif changes:
            actions.append({"do": "change", "id": u["id"], "changes": changes, **_fields(w)})

    # Leavers: people sync manages who are no longer wanted. Local people are never touched.
    for u in conn.execute("SELECT * FROM users WHERE tenant_code = ? AND ms_tenant = ? AND source = 'microsoft' "
                          "AND disabled = 0", (tenant, tid)).fetchall():
        if u["ms_oid"] not in wanted:
            actions.append({"do": "disable", "id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
                            "ext": u["ext"]})

    # Never leave the tenant without an active admin.
    after = {u["id"] for u in conn.execute("SELECT id FROM users WHERE tenant_code = ? AND role = 'admin' "
                                           "AND disabled = 0", (tenant,)).fetchall()}
    before = set(after)
    for a in actions:
        if a["do"] == "disable" or (a.get("id") and a["role"] != "admin"):
            after.discard(a.get("id"))
        elif a["role"] == "admin":
            after.add(a.get("id") or ("new", a["oid"]))
    if before and not after:
        for a in [a for a in actions if a.get("id") in before]:
            actions.remove(a)
            problems.append({"who": a["email"], "problem": "kept as an admin: your organisation needs at least one"})

    counts = {k: sum(1 for a in actions if a["do"] in d) for k, d in
              (("added", ("add", "link")), ("changed", ("change",)), ("disabled", ("disable",)),
               ("enabled", ("enable",)))}
    return {"at": iso(), "counts": counts, "actions": actions, "problems": problems, "members": len(members)}


def _fields(w: dict) -> dict:
    return {"email": w["email"], "name": w["name"], "role": w["role"], "ext": w["ext"], "ext_created": w["ext_created"],
            "oid": w["oid"]}


def _apply(conn, link, result: dict):
    now = iso()
    sync_user = {"id": None, "email": "microsoft-365-sync", "tenant_code": link["tenant_code"]}
    for a in result["actions"]:
        if a["do"] == "add":
            conn.execute("INSERT INTO users (email, name, tenant_code, role, ext, ext_created, created_at, updated_at, "
                         "source, ms_tenant, ms_oid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'microsoft', ?, ?)",
                         (a["email"], a["name"], link["tenant_code"], a["role"], a["ext"], a["ext_created"], now, now,
                          link["ms_tenant"], a["oid"]))
        elif a["do"] == "disable":
            conn.execute("UPDATE users SET disabled = 1, disabled_by = 'sync', updated_at = ? WHERE id = ?", (now, a["id"]))
            auth.end_all_sessions(conn, a["id"])
        else:  # link, change, enable
            u = conn.execute("SELECT * FROM users WHERE id = ?", (a["id"],)).fetchone()
            # From now on sync manages them (a linked local person keeps their password too).
            conn.execute("UPDATE users SET name = ?, role = ?, ext = ?, ext_created = ?, disabled = 0, disabled_by = NULL, "
                         "ms_tenant = ?, ms_oid = ?, source = 'microsoft', updated_at = ? WHERE id = ?",
                         (a["name"] or u["name"], a["role"], a["ext"], a["ext_created"], link["ms_tenant"], a["oid"],
                          now, a["id"]))
            if a["role"] != u["role"] or (a["ext"] or None) != (u["ext"] or None):
                auth.end_all_sessions(conn, a["id"])
        db.audit(conn, f"Microsoft 365 sync: {a['do']}", sync_user, "", f"{a['email']} ({a['role']}"
                 + (f", ext {a['ext']})" if a.get("ext") else ")"))


async def sync_loop():
    """Every linked tenant with a group, every SYNC_MINUTES, off the event
    loop (Graph and gwphones are called synchronously)."""
    while True:
        await asyncio.sleep(SYNC_MINUTES * 60)
        if not ready():
            continue
        with db.connect() as conn:
            codes = [r["tenant_code"] for r in conn.execute("SELECT tenant_code FROM ms_links WHERE group_id != ''")]
        for code in codes:
            try:
                result = await asyncio.to_thread(_run, code, True)
                if result.get("error"):
                    log.warning("Microsoft 365 sync for %s: %s", code, result["error"])
            except Exception:
                log.exception("Microsoft 365 sync for %s failed", code)


@app.on_event("startup")
async def _start_sync():
    if os.environ.get("PORTAL_DISABLE_BACKGROUND") != "1":
        asyncio.get_event_loop().create_task(sync_loop())
