"""Yealink RPS (Redirection and Provisioning Service): telling Yealink's
cloud where a desk phone should fetch its settings, so a new or
factory-reset phone sets itself up when it's plugged in.

From "Yealink Json API for RPS Management Platform" (V3.6.0.20):

- https://api-dm.yealink.com:8443 (PBX_RPS_API_URL), with an AccessKey ID
  and Secret from "Yealink Device Management Cloud Service for RPS
  Enterprise" (PBX_RPS_ACCESS_KEY, PBX_RPS_ACCESS_SECRET in .env). Without
  them, nothing here is used and phones are set up by hand.
- Each request carries X-Ca-Key, X-Ca-Timestamp (ms), X-Ca-Nonce (a fresh
  UUID: a nonce can't be reused within 5 minutes, and the clock must be
  within 5 minutes), Content-MD5 (base64 of the body's MD5, body requests
  only) and X-Ca-Signature: base64 of HMAC-SHA256 with the secret over
      METHOD \\n [Content-MD5:x \\n] X-Ca-Key:x \\n X-Ca-Nonce:x \\n
      X-Ca-Timestamp:x \\n api/path [\\n k=v&k=v (query, keys sorted)]
  with no newline at the end.
- Answers are {"ret", "data", "error": {"msg", "errorCode"}}; ret < 0 is
  an error, whose msg is a code like device.mac.added.by.other.
- A phone is added with its own provisioning URL and credentials
  (uniqueServerUrl, authName, password: at most 32 characters), so no
  server entry is needed. Its RPS id is kept to edit or remove it later.

The phone's provisioning password goes to Yealink: that's how RPS hands
it to the phone. Nothing else of ours does, and nothing secret is logged.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Optional

import httpx

API_URL = os.environ.get("PBX_RPS_API_URL", "https://api-dm.yealink.com:8443").rstrip("/")
ACCESS_KEY = os.environ.get("PBX_RPS_ACCESS_KEY", "").strip()
ACCESS_SECRET = os.environ.get("PBX_RPS_ACCESS_SECRET", "").strip()
# Each call. A phone's sync is up to three calls (add, list, edit), inside
# the request that saves it, and the portal waits 15 s for that answer.
TIMEOUT = 4

# What an RPS error code means, for the console and the portal.
MESSAGES = {
    "device.mac.added.by.other": "This phone is registered to another company in Yealink's service. Ask them to "
                                 "release it (or Yealink, at ticket.yealink.com), or set it up by hand.",
    "device.mac.invalid": "Yealink's service doesn't accept this MAC address.",
    "accesskey.id.invalid": "Yealink's service refused our access key: check PBX_RPS_ACCESS_KEY.",
    "request.header.invalid": "Yealink's service refused the request's signature: check PBX_RPS_ACCESS_SECRET.",
    "service.common.token.unauthorized": "Yealink's service refused our access key.",
    "request.replay": "Yealink's service refused the request's time: check the server's clock.",
    "url.invalid": "Yealink's service refused the provisioning address.",
}


class RpsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, f"Yealink's service said: {self.code}")


def ready() -> bool:
    return bool(ACCESS_KEY and ACCESS_SECRET)


def string_to_sign(method: str, path: str, headers: dict, query: Optional[dict] = None) -> str:
    lines = [method.upper()]
    if "Content-MD5" in headers:
        lines.append(f"Content-MD5:{headers['Content-MD5']}")
    lines += [f"X-Ca-Key:{headers['X-Ca-Key']}", f"X-Ca-Nonce:{headers['X-Ca-Nonce']}",
              f"X-Ca-Timestamp:{headers['X-Ca-Timestamp']}"]
    text = "\n".join(lines) + "\n" + path.lstrip("/")
    if query:
        text += "\n" + "&".join(k if str(v).strip() == "" else f"{k}={v}" for k, v in sorted(query.items()))
    return text


def sign(text: str, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha256).digest()).decode()


def _send(method: str, url: str, headers: dict, content: Optional[bytes], query: Optional[dict]) -> httpx.Response:
    with httpx.Client(timeout=TIMEOUT) as client:
        return client.request(method, url, headers=headers, content=content, params=query)


def request(method: str, path: str, body=None, query: Optional[dict] = None):
    """One signed call; the answer's data, or RpsError."""
    if not ready():
        raise RpsError("not.configured")
    headers = {"X-Ca-Key": ACCESS_KEY, "X-Ca-Timestamp": str(int(time.time() * 1000)), "X-Ca-Nonce": uuid.uuid4().hex}
    content = None
    if body is not None:
        content = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-MD5"] = base64.b64encode(hashlib.md5(content).digest()).decode()
        headers["Content-Type"] = "application/json"
    headers["X-Ca-Signature"] = sign(string_to_sign(method, path, headers, query), ACCESS_SECRET)
    try:
        r = _send(method, API_URL + path, headers, content, query)
        data = r.json()
    except httpx.HTTPError as e:
        raise RpsError(f"unreachable ({type(e).__name__})")
    except ValueError:
        raise RpsError(f"bad answer (HTTP {r.status_code})")
    if not isinstance(data, dict) or data.get("ret", -1) < 0:
        err = (data.get("error") or data.get("errors") or {}) if isinstance(data, dict) else {}
        raise RpsError(str(err.get("msg") or f"HTTP {r.status_code}")[:120])
    return data.get("data")


def _find(mac: str) -> str:
    """Our own device entry for a MAC, by listing with it as the key."""
    data = request("POST", "/api/open/v1/device/list", {"key": mac, "skip": 0, "limit": 10}) or {}
    for d in data.get("data") or []:
        if str(d.get("mac", "")).lower().replace(":", "") == mac:
            return d["id"]
    raise RpsError("device.not.found")


def register(mac: str, url: str, password: str, remark: str = "", rps_id: Optional[str] = None) -> str:
    """Adds the phone (or updates ours), pointing it at url with its own
    credentials. Returns its RPS id."""
    fields = {"uniqueServerUrl": url, "authName": mac, "password": password, "remark": remark[:256]}
    if not rps_id:
        try:
            data = request("POST", "/api/open/v1/device/add", {"macs": [mac], **fields})
            return data[0]["id"]
        except RpsError as e:
            if e.code != "device.mac.existed":  # already ours: update it below
                raise
        rps_id = _find(mac)
    try:
        request("POST", "/api/open/v1/device/edit", {"id": rps_id, **fields})
    except RpsError as e:
        if e.code != "device.not.found":
            raise
        # removed in RPS meanwhile: add it again (once)
        return request("POST", "/api/open/v1/device/add", {"macs": [mac], **fields})[0]["id"]
    return rps_id


def unregister(rps_id: str):
    request("POST", "/api/open/v1/device/delete", {"ids": [rps_id]})


def check(mac: str) -> dict:
    """Read-only, to try the connection before any phone is registered:
    a query request (is this MAC known, and whose) and a body request (our
    server entries), so both kinds of signing are tested."""
    out = {}
    for name, call in (("mac", lambda: request("GET", "/api/open/v1/device/checkMac", query={"mac": mac})),
                       ("device", lambda: request("GET", "/api/open/v1/device/checkDevice", query={"mac": mac})),
                       ("servers", lambda: request("POST", "/api/open/v1/server/list", {"skip": 0}))):
        try:
            out[name] = {"ok": True, "data": call()}
        except RpsError as e:
            out[name] = {"ok": False, "error": e.code, "message": e.message}
    return out
