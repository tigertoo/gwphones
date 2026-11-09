"""
Call links: a URL anyone can open to call an extension or ring group from
their browser (WebRTC), e.g. in an email signature or on a website.

The page at /call/<token> (public, like /prov/) carries the guest login,
which is therefore public: the endpoint it logs in as (pbx.WEB_GUEST) can
only reach [portal-weblinks], with one entry per live link (wl-<token>).
The browser connects to Asterisk over a WebSocket that nginx proxies at
/pbx-ws; the audio (and video, if the link allows it) goes straight to
Asterisk's public address over its usual RTP ports, encrypted (DTLS-SRTP).

Limits are in the dialplan (calls at once per link and in all, maximum
length) and in nginx (requests per IP), since Asterisk sees every browser
as nginx on 127.0.0.1. The page never calls by itself: the visitor
presses Call.
"""

import json
import os
import re
import secrets
import time
from collections import defaultdict, deque

import segno
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from pbx import (NAME_RE, PROVISION_BASE_URL, PUBLIC_HOST, WEB_GUEST, _client_ip, _lock, _now,
                 apply_config, changes_log, check_target, current_tenant, get_db, get_settings, live_call_links,
                 needs_keypad,
                 web_guest_password)

router = APIRouter(prefix="/api/pbx")
public_router = APIRouter(prefix="/call")

# Where browsers open the SIP WebSocket: nginx's /pbx-ws on the public site.
WS_URL = os.environ.get("PBX_WEBRTC_WS_URL", "") or (
    "wss://" + PROVISION_BASE_URL.split("://", 1)[-1] + "/pbx-ws" if PROVISION_BASE_URL else "")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "call")
STATIC_FILES = {"call.js": "text/javascript", "call.css": "text/css", "jssip-3.13.8.min.js": "text/javascript"}
MAX_MINUTES = 120

PAGE_LOADS_PER_IP = 30  # per 10 minutes; nginx limits too
_loads = defaultdict(deque)


def _too_many(ip: str) -> bool:
    q = _loads[ip]
    now = time.monotonic()
    while q and q[0] < now - 600:
        q.popleft()
    q.append(now)
    return len(q) > PAGE_LOADS_PER_IP


def _link_url(token: str) -> str:
    return f"{PROVISION_BASE_URL}/call/{token}" if PROVISION_BASE_URL else ""


# ---------------------------------------------------------------------------
# Dashboard side
# ---------------------------------------------------------------------------

class CallLinkRequest(BaseModel):
    label: str
    target: str
    video: bool = False
    max_minutes: int = 30
    enabled: bool = True


def _clean(conn, req: CallLinkRequest) -> tuple:
    label = req.label.strip()
    if not NAME_RE.match(label):
        raise HTTPException(400, "Give the link a short name (letters, numbers, spaces, . ' _ -), e.g. Sales website.")
    target = check_target(conn, req.target)
    if not 1 <= req.max_minutes <= MAX_MINUTES:
        raise HTTPException(400, f"The maximum call length must be 1-{MAX_MINUTES} minutes.")
    return label, target


def _row(conn, r, live_ids) -> dict:
    url = _link_url(r["token"])
    return {"id": r["id"], "label": r["label"], "target": r["target"], "video": bool(r["video"]),
            "max_minutes": r["max_minutes"], "enabled": bool(r["enabled"]), "live": r["id"] in live_ids,
            "created_at": r["created_at"], "last_opened_at": r["last_opened_at"], "url": url,
            "qr": segno.make(url, error="m").svg_data_uri(scale=5, border=2) if url else ""}


@router.get("/call-links")
def api_call_links():
    with get_db() as conn:
        live_ids = {r["id"] for r in live_call_links(conn)}
        links = [_row(conn, r, live_ids) for r in conn.execute("SELECT * FROM call_links WHERE tenant_id = ? ORDER BY id",
                                                                (current_tenant(),))]
        video_on = get_settings(conn)["video"] == "1"
    return {"links": links, "ready": bool(PROVISION_BASE_URL and PUBLIC_HOST and WS_URL), "ws_url": WS_URL,
            "video_on": video_on, "max_minutes": MAX_MINUTES}


@router.post("/call-links")
def api_add_call_link(req: CallLinkRequest):
    with _lock, get_db() as conn:
        label, target = _clean(conn, req)
        conn.execute("INSERT INTO call_links (token, label, target, video, max_minutes, enabled, created_at, tenant_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (secrets.token_urlsafe(16), label, target, int(req.video), req.max_minutes, int(req.enabled), _now(),
                      current_tenant()))
        conn.commit()
        return apply_config(conn, f"Added call link {label} to {target}")


@router.put("/call-links/{link_id}")
def api_update_call_link(link_id: int, req: CallLinkRequest):
    with _lock, get_db() as conn:
        label, target = _clean(conn, req)
        if conn.execute("UPDATE call_links SET label = ?, target = ?, video = ?, max_minutes = ?, enabled = ? "
                        "WHERE id = ? AND tenant_id = ?",
                        (label, target, int(req.video), req.max_minutes, int(req.enabled), link_id,
                         current_tenant())).rowcount == 0:
            raise HTTPException(404, "Call link not found.")
        conn.commit()
        return apply_config(conn, f"Call link {link_id}: {label} to {target}, video={req.video}, "
                                  f"max {req.max_minutes} min, enabled={req.enabled}")


@router.post("/call-links/{link_id}/new-url")
def api_call_link_new_url(link_id: int):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE call_links SET token = ? WHERE id = ? AND tenant_id = ?",
                        (secrets.token_urlsafe(16), link_id, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Call link not found.")
        conn.commit()
        return apply_config(conn, f"New URL for call link {link_id}: the old one stops working")


@router.delete("/call-links/{link_id}")
def api_delete_call_link(link_id: int):
    with _lock, get_db() as conn:
        if conn.execute("DELETE FROM call_links WHERE id = ? AND tenant_id = ?", (link_id, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Call link not found.")
        conn.commit()
        return apply_config(conn, f"Deleted call link {link_id}")


# ---------------------------------------------------------------------------
# The public page
# ---------------------------------------------------------------------------

def _headers() -> dict:
    ws_origin = WS_URL.split("/pbx-ws")[0] if WS_URL else "'none'"
    return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex",
            "X-Content-Type-Options": "nosniff",
            # Scripts only from here; framing allowed so a website can embed it.
            "Content-Security-Policy": f"default-src 'self'; script-src 'self'; style-src 'self'; "
                                       f"connect-src 'self' {ws_origin}; img-src 'self' data:; "
                                       f"media-src 'self' blob: mediastream:; frame-ancestors *; "
                                       f"base-uri 'none'; form-action 'none'"}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/call/static/call.css"></head>
<body><main class="card">{body}</main></body></html>""", status_code=status, headers=_headers())


@public_router.get("/static/{name}")
def call_static(name: str):
    if name not in STATIC_FILES:
        raise HTTPException(404)
    return FileResponse(os.path.join(STATIC_DIR, name), media_type=STATIC_FILES[name],
                        headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"})


KEYPAD = ('<div id="keypad" class="keypad" hidden>' +
          "".join(f'<button type="button" data-key="{k}">{k}</button>' for k in "123456789*0#") + "</div>")


@public_router.get("/{token}", response_class=HTMLResponse)
def call_page(token: str, request: Request):
    ip = _client_ip(request)
    if _too_many(ip):
        return _page("Try again later", "<h1>Too many attempts</h1><p>Please try again in a few minutes.</p>", 429)
    with _lock, get_db() as conn:
        link = next((r for r in live_call_links(conn) if secrets.compare_digest(r["token"], token)), None)
        if not link or not (WS_URL and PUBLIC_HOST):
            return _page("Not available", "<h1>This call link isn't available</h1>"
                                          "<p>It may have been switched off or replaced. Please contact us another way.</p>", 404)
        conn.execute("UPDATE call_links SET last_opened_at = ? WHERE id = ?", (_now(), link["id"]))
        conn.commit()
        password = web_guest_password(conn)
        keypad = needs_keypad(conn, link["target"], link["tenant_id"])  # a menu: the visitor presses keys
    config = {"ws": WS_URL, "domain": PUBLIC_HOST, "user": WEB_GUEST, "password": password,
              "target": f"wl-{token}", "label": link["label"], "video": bool(link["video"]),
              "maxMinutes": link["max_minutes"]}
    # JSON in a script element, with "</" escaped so it can't close it.
    data = json.dumps(config).replace("</", "<\\/")
    label = re.sub(r"[^A-Za-z0-9 .'_-]", "", link["label"])
    video = ('<label class="row"><input type="checkbox" id="video"> Turn on my camera</label>'
             if link["video"] else "")
    return _page(f"Call {label}", f"""
<h1>Call {label}</h1>
<p class="hint">From this browser, using your microphone{' (and camera, if you turn it on)' if link['video'] else ''}.</p>
<label class="field">Your name (optional)<input id="name" maxlength="24" autocomplete="name"></label>
{video}
<div class="buttons">
  <button id="call" class="call">Call</button>
  <button id="hangup" class="hangup" hidden>Hang up</button>
  <button id="mute" hidden>Mute</button>
</div>
<p id="status" class="status" role="status" aria-live="polite"></p>
{KEYPAD if keypad else ''}
<div id="videos" class="videos" hidden><video id="remote" autoplay playsinline></video><video id="local" autoplay playsinline muted></video></div>
<audio id="audio" autoplay></audio>
<p class="hint small">Calls are limited to {link['max_minutes']} minutes. In an app's built-in browser (Gmail, LinkedIn)
the microphone may be blocked: open the link in Safari or Chrome.</p>
<script type="application/json" id="config">{data}</script>
<script src="/call/static/jssip-3.13.8.min.js"></script>
<script src="/call/static/call.js"></script>""")
