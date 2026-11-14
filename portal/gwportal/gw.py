"""Talking to gwphones as the portal caller: its secret, the user's tenant
by code, and who's acting (for gwphones' change log), on every request.

gwphones' replies are never passed on whole: phone.py picks fields. Its
error messages are only passed on when they're plain answers about the
request (e.g. "That doesn't look like an email address."); anything that
could mention its own setup becomes a message of ours. A save gwphones
couldn't apply to Asterisk yet comes back as saved-but-pending, and is
logged for the operator.
"""
import logging
import re

import httpx

from . import config
from .auth import Refused

log = logging.getLogger("gwportal")

# Replaced in tests by a client for the real gwphones app. No cookies are
# ever kept: everything a request needs is in its headers.
client = None

UNAVAILABLE = "The phone system can't be reached right now. Try again in a minute."
_INTERNAL = re.compile(r"\.env|dashboard|asterisk|reload|MS_GRAPH|EMAIL_FROM|PBX_|operator|server|trunk", re.I)


def _client():
    global client
    if client is None:
        client = httpx.Client(base_url=config.GW_URL, timeout=15)
    return client


class Pending:
    """A save that worked but isn't live in Asterisk yet; data is the rest
    of gwphones' answer (e.g. the number a new group was given)."""
    def __init__(self, data=None):
        self.data = data or {}


def call(user, method: str, path: str, json=None, params=None, raw: bool = False, content: bytes = None):
    """content: a raw body (audio) instead of JSON."""
    headers = {"X-Portal-Secret": config.GW_SECRET, "X-Portal-Tenant": user["tenant_code"],
               "X-Portal-User": user["email"]}
    if content is not None:
        headers["Content-Type"] = "application/octet-stream"
    try:
        c = _client()
        c.cookies.clear()
        r = c.request(method, f"/api/pbx{path}", json=json, params=params, headers=headers, content=content)
    except httpx.HTTPError as e:
        log.warning("gwphones unreachable: %s", type(e).__name__)
        raise Refused(UNAVAILABLE, 503)
    if r.status_code >= 400:
        raise Refused(*_error(r))
    if raw:
        return r
    data = r.json()
    if isinstance(data, dict) and data.get("ok") is False:
        log.warning("Saved but not applied for tenant %s: %s", user["tenant_code"], "; ".join(data.get("messages") or []))
        return Pending(data)
    return data


def _error(r):
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = None
    detail = detail if isinstance(detail, str) else ""
    if r.status_code == 403 and detail.startswith("This account is suspended"):
        return detail, 403
    if "MS_GRAPH" in detail or "Email not sent" in detail:
        log.warning("gwphones couldn't email: %s", detail[:200])
        return "Emails can't be sent right now. Try again later, or use a setup link instead.", 503
    if r.status_code in (400, 404, 409) and detail and not _INTERNAL.search(detail):
        return detail, r.status_code
    if r.status_code == 404:
        return "Not found.", 404
    if r.status_code in (400, 409, 422):
        return "That couldn't be done. Check what you entered and try again.", 400
    log.warning("gwphones answered %s: %s", r.status_code, detail[:200])
    return (UNAVAILABLE, 503) if r.status_code >= 500 or r.status_code in (401, 403) else ("That couldn't be done.", 400)
