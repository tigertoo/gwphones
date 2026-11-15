"""
Desk phones (Yealink), set up through Yealink's redirection service (RPS).

Each phone added here gets its own provisioning username (its MAC) and
password. In RPS the phone's MAC points at a server entry with
<DESKPHONE_BASE_URL>/prov/yealink/ and those credentials, so a
phone out of the box asks RPS where to go, then fetches <mac>.cfg here.
It keeps the URL and credentials and fetches again every
REPEAT_MINUTES, so a renamed, disabled or re-passworded extension reaches
the phone without anyone touching it.

The phone sends no credentials until it's challenged, so any request
without valid ones gets a 401 with WWW-Authenticate. Valid credentials
only fetch that same phone's file, never another MAC's. Failed attempts
are counted per IP and too many get a 429 for a while.

Only <mac>.cfg is served; everything else a Yealink asks for
(y0000000000XX.cfg, <mac>-local.cfg, <mac>.boot...) gets a 404, which the
phone treats as "nothing there". Key names follow Yealink's
auto-provisioning guide for T3x/T4x/T5x (firmware 8x); check a new model
against "view config" before relying on it. The older T41P/T46G (end of
life, last firmware 8x) use the same names.

Line keys: key 1 is the phone's own line; the next ones are busy lamps
(BLF) for the other extensions, in order, and the rest up to
MAX_LINE_KEYS are cleared, so a removed extension's key goes. A phone
ignores keys it doesn't have (a T41P shows 6 a page).

Tenants: each phone belongs to the tenant it was added in (tenant_id) and
is that tenant's through and through: it logs in as the tenant's login
(1002201), registers to the tenant's SIP hostname, and its busy lamps are
the tenant's own extensions (its endpoint subscribes to the tenant's
hints, which are bare extensions). The dashboard only lists and changes
the phones of the tenant it's working on. A MAC is unique on the server.

The address: a tenant with its own SIP hostname gets
https://<that hostname>/prov/yealink/, the same name its phones register
to (so it can move with that tenant by DNS); others get
PBX_DESKPHONE_BASE_URL (falls back to PBX_PROVISION_BASE_URL). nginx's
pbx-prov site has to answer for *.sip.<domain> too. A phone's file is
never served under another tenant's hostname.

Firmware (server-wide, the operator's): .rom files uploaded in the console
are kept in FIRMWARE_DIR, and each model can have a target version. A
phone's user agent says what it runs ("Yealink SIP-T46G 28.83.0.120
<mac>"); only when that model has a target and the phone runs something
else does its config carry static.firmware.url, with the phone's own
credentials in it, so a phone already on the target never downloads
again. A phone that doesn't say what it runs isn't touched. If a phone is
still on something else after FIRMWARE_TRIES configs with the line (the
file isn't the version it was labelled, or the phone refuses it), it's
left alone and the console shows it as stuck, rather than downloading it
every hour; choosing the target again tries again. The files are
served only to a phone with valid credentials.
It must be a site with an RSA certificate and DHE-RSA ciphers: a T46G on
its last firmware (28.83) speaks TLS 1.2 but offers no ECDHE ciphers, so
against an ECDSA certificate the handshake fails before any request
(alert 40, handshake_failure, and nothing in the access log).
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from typing import Optional

from pydantic import BaseModel

from pbx import (PBX_DB_PATH, PROVISION_BASE_URL, PUBLIC_HOST, PUBLIC_PORT, _client_ip, _lock, _now,
                 changes_log, current_tenant, get_db, sip_host_for, tenant_names)

router = APIRouter(prefix="/api/pbx")
public_router = APIRouter(prefix="/prov/yealink")

MAC_RE = re.compile(r"^[0-9a-f]{12}$")
MODELS = ["T41P", "T46G", "T31P", "T31G", "T33G", "T34W", "T43U", "T44U", "T46U", "T48U", "T53", "T53W", "T54W", "T57W", "T58W"]
REPEAT_MINUTES = 60
VOICEMAIL_NUMBER = "*97"
MAX_LINE_KEYS = 27  # a T46G/T46U/T54W's 27 (3 pages of 9 or 10); more models have fewer
PICKUP_PREFIX = "**"  # **2XX answers that extension's ringing call (pbx.py, feature codes)
DESKPHONE_BASE_URL = os.environ.get("PBX_DESKPHONE_BASE_URL", "").rstrip("/") or PROVISION_BASE_URL

FIRMWARE_DIR = os.environ.get("PBX_FIRMWARE_DIR") or os.path.join(os.path.dirname(PBX_DB_PATH) or ".", "firmware")
FIRMWARE_MAX_BYTES = 100 * 1024 * 1024  # a T4x .rom is about 25 MB, a T5x about 80
FIRMWARE_MIN_BYTES = 1024 * 1024
FIRMWARE_TRIES = 3  # configs with the firmware line before giving up on a phone (a wrong version label)
VERSION_RE = re.compile(r"^\d{1,4}(\.\d{1,4}){2,4}$")
AGENT_RE = re.compile(r"^Yealink SIP-(\S+) (\d{1,4}(?:\.\d{1,4}){2,4})\b")

MAX_FAILURES = 10  # per IP per FAILURE_WINDOW seconds, then 429
FAILURE_WINDOW = 600
_failures = defaultdict(deque)
_NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


def clean_mac(value: str) -> str:
    mac = re.sub(r"[\s:.-]", "", value or "").lower()
    if not MAC_RE.match(mac):
        raise HTTPException(400, "MAC address must be 12 hex digits, e.g. 80:5E:C0:12:34:56 (on the sticker under the phone).")
    return mac


def _pretty(mac: str) -> str:
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2)).upper()


def agent_firmware(agent: Optional[str]):
    """(model, version) from a Yealink's user agent, or (None, None)."""
    m = AGENT_RE.match(agent or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def firmware_target(conn, model: str):
    return conn.execute("SELECT f.* FROM desk_firmware_targets t JOIN desk_firmware f ON f.id = t.firmware_id "
                        "WHERE t.model = ?", (model,)).fetchone()


def firmware_due(conn, phone, agent: Optional[str]):
    """(the target this phone should install, or None; whether it's stuck:
    sent FIRMWARE_TRIES times and still not on it)."""
    model, version = agent_firmware(agent)
    fw = firmware_target(conn, model) if model else None
    if not fw:
        return None, False
    if fw["version"] == version:
        return None, False
    r = conn.execute("SELECT count FROM desk_firmware_attempts WHERE mac = ? AND firmware_id = ?",
                     (phone["mac"], fw["id"])).fetchone()
    return fw, bool(r and r["count"] >= FIRMWARE_TRIES)


def _firmware_lines(conn, phone, agent: Optional[str], fetching: bool) -> list:
    """static.firmware.url only for a phone that says it runs something
    other than its model's target, and isn't stuck. A real fetch counts
    the try; reporting the target clears them."""
    model, version = agent_firmware(agent)
    if fetching and model:
        conn.execute("DELETE FROM desk_firmware_attempts WHERE mac = ? AND firmware_id IN "
                     "(SELECT id FROM desk_firmware WHERE model = ? AND version = ?)", (phone["mac"], model, version))
    fw, stuck = firmware_due(conn, phone, agent)
    base = server_url(phone["tenant_id"])
    if not fw or not base:
        return []
    if stuck:
        if fetching and conn.execute("UPDATE desk_firmware_attempts SET count = count + 1 WHERE mac = ? AND firmware_id = ? "
                                     "AND count = ?", (phone["mac"], fw["id"], FIRMWARE_TRIES)).rowcount:
            changes_log.info(f"Desk phone {_pretty(phone['mac'])} still on {version} after {FIRMWARE_TRIES} tries at "
                             f"{fw['name']}: left alone. Check the file is the version it was labelled.")
        return []
    if fetching:
        conn.execute("INSERT INTO desk_firmware_attempts (mac, firmware_id, count) VALUES (?, ?, 1) "
                     "ON CONFLICT(mac, firmware_id) DO UPDATE SET count = count + 1", (phone["mac"], fw["id"]))
    scheme, rest = base.split("://", 1)
    return [f"static.firmware.url = {scheme}://{phone['mac']}:{phone['prov_pass']}@{rest}firmware/{fw['name']}", ""]


def build_config(conn, phone, agent: Optional[str] = None, fetching: bool = False) -> str:
    """The phone's <mac>.cfg, for a phone with this user agent. An
    extension that's gone or turned off is sent as a disabled account,
    which logs the phone out."""
    e = conn.execute("SELECT * FROM extensions WHERE ext = ? AND tenant_id = ?",
                     (phone["ext"], phone["tenant_id"])).fetchone()
    lines = ["#!version:1.0.0.1",
             "## Generated by the admin dashboard (Phone System tab, Desk phones).",
             "## Changes made on the phone are overwritten on its next check.",
             "",
             "static.auto_provision.repeat.enable = 1",
             f"static.auto_provision.repeat.minutes = {REPEAT_MINUTES}",
             f"static.security.user_password = admin:{phone['admin_pass']}",
             # Only Accept Trusted Certificates: checked on the T46G (28.83), which
             # trusts Let's Encrypt's root, so a phone set up with it off turns it on.
             "static.security.trust_certificates = 1",
             # Common Name Validation: the certificate must be for the server it
             # reaches, not just any trusted one. Checked on the T46G against
             # pbx.amapforyourmoney.com and a tenant's *.sip wildcard hostname.
             "static.security.cn_validation = 1",
             "local_time.time_zone = 0",
             "local_time.time_zone_name = Ireland(Dublin)",
             "local_time.summer_time = 2",
             "local_time.ntp_server1 = pool.ntp.org",
             ""]
    lines += _firmware_lines(conn, phone, agent, fetching)
    if not e or not e["enabled"]:
        return "\n".join(lines + ["account.1.enable = 0", ""])
    name = re.sub(r"[\r\n]", " ", e["name"])
    login = tenant_names(conn, phone["tenant_id"]).login(e["ext"])
    lines += ["account.1.enable = 1",
              f"account.1.label = {name}",
              f"account.1.display_name = {name}",
              f"account.1.auth_name = {login}",
              f"account.1.user_name = {login}",
              f"account.1.password = {e['password']}",
              f"account.1.sip_server.1.address = {sip_host_for(phone['tenant_id'])}",
              f"account.1.sip_server.1.port = {PUBLIC_PORT}",
              "account.1.sip_server.1.transport_type = 2",  # TLS
              "account.1.sip_server.1.expires = 300",
              "account.1.srtp_encryption = 2",  # compulsory, as for the apps
              f"voice_mail.number.1 = {VOICEMAIL_NUMBER}",
              ""]
    return "\n".join(lines + _line_keys(conn, e["ext"], phone["tenant_id"]))


def _line_keys(conn, own: str, tenant: int) -> list:
    """Busy lamps for the other extensions: lit while that one is on a
    call, flashing while it rings; pressing it calls it, or answers it
    while it rings."""
    others = [r for r in conn.execute("SELECT ext, name FROM extensions WHERE enabled = 1 AND ext != ? AND tenant_id = ? "
                                      "ORDER BY ext", (own, tenant))][:MAX_LINE_KEYS - 1]
    lines = ["features.pickup.blf_visual_enable = 1", ""]
    for n, r in enumerate(others, start=2):
        name = re.sub(r"[\r\n]", " ", r["name"])
        lines += [f"linekey.{n}.type = 16",  # BLF
                  f"linekey.{n}.line = 1",
                  f"linekey.{n}.value = {r['ext']}",
                  f"linekey.{n}.label = {r['ext']} {name}",
                  f"linekey.{n}.pickup_value = {PICKUP_PREFIX}{r['ext']}"]
    for n in range(len(others) + 2, MAX_LINE_KEYS + 1):
        lines += [f"linekey.{n}.type = 0", f"linekey.{n}.value = ", f"linekey.{n}.label = "]
    return lines + [""]


def _too_many(ip: str) -> bool:
    """Also forgets an IP once its failures have expired, so scanners
    don't make the table grow for ever."""
    q = _failures.get(ip)
    if q is None:
        return False
    while q and q[0] < time.monotonic() - FAILURE_WINDOW:
        q.popleft()
    if not q:
        _failures.pop(ip, None)
        return False
    return len(q) >= MAX_FAILURES


def _challenge(ip: str, counts: bool = True):
    if counts:
        _failures[ip].append(time.monotonic())
    return Response("Authentication required.", status_code=401, media_type="text/plain",
                    headers={**_NO_STORE, "WWW-Authenticate": 'Basic realm="phone provisioning"'})


def _credentials(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
    except Exception:
        return None
    return user.lower(), password


@public_router.get("/{filename}")
def prov_yealink(filename: str, request: Request):
    ip = _client_ip(request)
    if _too_many(ip):
        return Response("Too many attempts. Try again later.", status_code=429, media_type="text/plain",
                        headers={**_NO_STORE, "Retry-After": str(FAILURE_WINDOW)})
    m = re.fullmatch(r"([0-9a-f]{12})\.cfg", filename.lower())
    if not m:
        return Response("Not found.", status_code=404, media_type="text/plain", headers=_NO_STORE)
    creds = _credentials(request)
    if not creds:
        return _challenge(ip, counts=False)  # the phone's first try never has them
    user, password = creds
    with _lock, get_db() as conn:
        phone = conn.execute("SELECT * FROM desk_phones WHERE mac = ?", (m.group(1),)).fetchone()
        # A phone's credentials only open its own file.
        if not phone or not hmac.compare_digest(user.encode(), phone["mac"].encode()) \
                or not hmac.compare_digest(password.encode(), phone["prov_pass"].encode()) \
                or _host_refused(conn, request, phone["tenant_id"]):
            changes_log.info(f"Desk phone config for {_pretty(m.group(1))} refused from {ip}: wrong credentials")
            return _challenge(ip)
        agent = request.headers.get("user-agent", "")[:160]
        conn.execute("UPDATE desk_phones SET last_fetch_at = ?, last_fetch_ip = ?, last_fetch_agent = ? WHERE mac = ?",
                     (_now(), ip, agent, phone["mac"]))
        body = build_config(conn, phone, agent, fetching=True)
        conn.commit()
    was, now = agent_firmware(phone["last_fetch_agent"])[1], agent_firmware(agent)[1]
    if was and now and was != now:
        changes_log.info(f"Desk phone {_pretty(phone['mac'])} firmware {was} -> {now}")
    upgrade = re.search(r"^static\.firmware\.url = .*/firmware/(\S+)$", body, flags=re.M)
    if upgrade:
        changes_log.info(f"Desk phone {_pretty(phone['mac'])} on {now} told to upgrade to {upgrade.group(1)}")
    if phone["last_fetch_ip"] != ip:  # every hour otherwise; log only what's new
        changes_log.info(f"Desk phone {_pretty(phone['mac'])} (tenant {phone['tenant_id']}, ext {phone['ext']}) fetched its config "
                         f"from {ip} ({agent or 'no user agent'})")
    return PlainTextResponse(body, headers=_NO_STORE)


# ---------------------------------------------------------------------------
# Dashboard side
# ---------------------------------------------------------------------------

class DeskPhoneRequest(BaseModel):
    mac: str = ""
    ext: str
    model: str = ""


def _ext_exists(conn, ext: str) -> str:
    if not conn.execute("SELECT 1 FROM extensions WHERE ext = ? AND tenant_id = ?", (ext, current_tenant())).fetchone():
        raise HTTPException(400, f"There's no extension {ext}.")
    return ext


def _ext_free(conn, ext: str, mac: str = ""):
    """One phone per extension: an extension takes one device at a time
    (aor-ext, max_contacts 1), so two phones on it would keep logging each
    other out."""
    r = conn.execute("SELECT mac FROM desk_phones WHERE ext = ? AND tenant_id = ? AND mac != ?",
                     (ext, current_tenant(), mac)).fetchone()
    if r:
        raise HTTPException(409, f"Phone {_pretty(r['mac'])} is already on extension {ext}. "
                                 "Give each desk phone its own extension.")


def server_url(tenant: int) -> str:
    """Where the tenant's phones fetch their file."""
    if not DESKPHONE_BASE_URL:
        return ""
    host = sip_host_for(tenant)
    if host and host != PUBLIC_HOST:
        return f"https://{host}/prov/yealink/"
    return f"{DESKPHONE_BASE_URL}/prov/yealink/"


def _host_refused(conn, request: Request, tenant: int) -> bool:
    """The request came in under another tenant's own hostname."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    r = conn.execute("SELECT id FROM tenants WHERE sip_host = ? AND sip_host != ''", (host,)).fetchone()
    return bool(r and r["id"] != tenant)


def _row(r, conn=None) -> dict:
    model, version = agent_firmware(r["last_fetch_agent"])
    fw, stuck = firmware_due(conn, r, r["last_fetch_agent"]) if conn is not None else (None, False)
    return {"firmware": version, "upgrade_to": fw["version"] if fw else None, "upgrade_stuck": stuck,
            "mac": r["mac"], "mac_pretty": _pretty(r["mac"]), "ext": r["ext"], "model": r["model"],
            "username": r["mac"], "password": r["prov_pass"], "admin_password": r["admin_pass"],
            "created_at": r["created_at"], "last_fetch_at": r["last_fetch_at"], "last_fetch_ip": r["last_fetch_ip"],
            "last_fetch_agent": r["last_fetch_agent"]}


@router.get("/desk-phones")
def api_desk_phones():
    with get_db() as conn:
        phones = [_row(r, conn) for r in conn.execute("SELECT * FROM desk_phones WHERE tenant_id = ? ORDER BY ext, mac",
                                                 (current_tenant(),))]
    return {"phones": phones, "models": MODELS, "repeat_minutes": REPEAT_MINUTES,
            "server_url": server_url(current_tenant()),
            "ready": bool(DESKPHONE_BASE_URL and PUBLIC_HOST)}


@router.post("/desk-phones")
def api_add_desk_phone(req: DeskPhoneRequest):
    mac = clean_mac(req.mac)
    with _lock, get_db() as conn:
        ext = _ext_exists(conn, req.ext.strip())
        _ext_free(conn, ext)
        other = conn.execute("SELECT tenant_id FROM desk_phones WHERE mac = ?", (mac,)).fetchone()
        if other and other["tenant_id"] == current_tenant():
            raise HTTPException(409, f"Phone {_pretty(mac)} is already added.")
        if other:  # another tenant's: don't say so
            changes_log.info(f"Desk phone {_pretty(mac)} not added for tenant {current_tenant()}: it's another tenant's")
            raise HTTPException(409, f"Phone {_pretty(mac)} can't be added. Check the MAC, or contact support.")
        conn.execute("INSERT INTO desk_phones (mac, ext, model, prov_pass, admin_pass, created_at, tenant_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (mac, ext, req.model.strip()[:20], secrets.token_urlsafe(18), secrets.token_urlsafe(9), _now(),
                      current_tenant()))
        conn.commit()
    changes_log.info(f"Added desk phone {_pretty(mac)} for extension {ext}")
    return {"ok": True, "message": f"Phone {_pretty(mac)} added. Now add it in Yealink RPS."}


@router.put("/desk-phones/{mac}")
def api_update_desk_phone(mac: str, req: DeskPhoneRequest):
    mac = clean_mac(mac)
    with _lock, get_db() as conn:
        ext = _ext_exists(conn, req.ext.strip())
        _ext_free(conn, ext, mac)
        if conn.execute("UPDATE desk_phones SET ext = ?, model = ? WHERE mac = ? AND tenant_id = ?",
                        (ext, req.model.strip()[:20], mac, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Phone not found.")
        conn.commit()
    changes_log.info(f"Desk phone {_pretty(mac)} now extension {ext}")
    return {"ok": True, "message": f"Saved. The phone picks it up within {REPEAT_MINUTES} minutes, or when it restarts."}


@router.post("/desk-phones/{mac}/new-password")
def api_desk_phone_password(mac: str):
    mac = clean_mac(mac)
    with _lock, get_db() as conn:
        if conn.execute("UPDATE desk_phones SET prov_pass = ? WHERE mac = ? AND tenant_id = ?",
                        (secrets.token_urlsafe(18), mac, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Phone not found.")
        conn.commit()
    changes_log.info(f"New provisioning password for desk phone {_pretty(mac)}")
    return {"ok": True, "message": "New password made. Put it in RPS, then factory-reset the phone so it fetches again."}


@router.delete("/desk-phones/{mac}")
def api_delete_desk_phone(mac: str):
    mac = clean_mac(mac)
    with _lock, get_db() as conn:
        if conn.execute("DELETE FROM desk_phones WHERE mac = ? AND tenant_id = ?", (mac, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Phone not found.")
        conn.execute("DELETE FROM desk_firmware_attempts WHERE mac = ?", (mac,))
        conn.commit()
    changes_log.info(f"Removed desk phone {_pretty(mac)}")
    return {"ok": True, "message": f"Phone {_pretty(mac)} removed. It keeps its last settings until it's reset: "
                               "give its extension a new password if the phone has left your hands."}


@router.get("/desk-phones/{mac}/config.cfg")
def api_desk_phone_config(mac: str):
    """What the phone gets, with the passwords hidden, for checking."""
    mac = clean_mac(mac)
    with get_db() as conn:
        phone = conn.execute("SELECT * FROM desk_phones WHERE mac = ? AND tenant_id = ?",
                             (mac, current_tenant())).fetchone()
        if not phone:
            raise HTTPException(404, "Phone not found.")
        body = build_config(conn, phone, phone["last_fetch_agent"])
    body = re.sub(r"^(account\.1\.password = ).*$", r"\1********", body, flags=re.M)
    body = re.sub(r"^(static\.firmware\.url = \w+://)[^@/]+@", r"\1********@", body, flags=re.M)
    body = re.sub(r"^(static\.security\.user_password = admin:).*$", r"\1********", body, flags=re.M)
    return PlainTextResponse(body, headers=_NO_STORE)


# ---------------------------------------------------------------------------
# Firmware (server-wide: the console only, never the portal)
# ---------------------------------------------------------------------------

@public_router.get("/firmware/{name}")
def prov_firmware(name: str, request: Request):
    """A library file, for any phone with valid credentials."""
    ip = _client_ip(request)
    if _too_many(ip):
        return Response("Too many attempts. Try again later.", status_code=429, media_type="text/plain",
                        headers={**_NO_STORE, "Retry-After": str(FAILURE_WINDOW)})
    creds = _credentials(request)
    if not creds:
        return _challenge(ip, counts=False)
    user, password = creds
    with get_db() as conn:
        phone = conn.execute("SELECT * FROM desk_phones WHERE mac = ?", (user,)).fetchone()
        if not phone or not hmac.compare_digest(password.encode(), phone["prov_pass"].encode()):
            changes_log.info(f"Firmware {name[:60]} refused from {ip}: wrong credentials")
            return _challenge(ip)
        fw = conn.execute("SELECT * FROM desk_firmware WHERE name = ?", (name,)).fetchone()
    path = os.path.join(FIRMWARE_DIR, fw["name"]) if fw else None
    if not path or not os.path.isfile(path):
        return Response("Not found.", status_code=404, media_type="text/plain", headers=_NO_STORE)
    changes_log.info(f"Desk phone {_pretty(phone['mac'])} downloading firmware {fw['name']} from {ip}")
    return FileResponse(path, media_type="application/octet-stream", filename=fw["name"], headers=_NO_STORE)


def _fw_row(r) -> dict:
    return {k: r[k] for k in ("id", "model", "version", "name", "size", "sha256", "uploaded_at")}


@router.get("/desk-firmware")
def api_firmware():
    with get_db() as conn:
        files = [_fw_row(r) for r in conn.execute("SELECT * FROM desk_firmware ORDER BY model, version")]
        targets = {r["model"]: r["firmware_id"] for r in conn.execute("SELECT * FROM desk_firmware_targets")}
        fleet = {}  # every tenant's phones, by model and what they run
        for r in conn.execute("SELECT last_fetch_agent FROM desk_phones"):
            model, version = agent_firmware(r["last_fetch_agent"])
            if model:
                fleet.setdefault(model, {}).setdefault(version, 0)
                fleet[model][version] += 1
    return {"files": files, "targets": targets, "fleet": fleet, "models": MODELS,
            "max_mb": FIRMWARE_MAX_BYTES // (1024 * 1024)}


@router.post("/desk-firmware")
async def api_upload_firmware(request: Request, model: str, version: str):
    """The .rom as the request body."""
    model, version = model.strip(), version.strip()
    if model not in MODELS:
        raise HTTPException(400, "Choose the phone model the firmware is for.")
    if not VERSION_RE.match(version):
        raise HTTPException(400, "Enter the version as the phone shows it, e.g. 28.83.0.120.")
    name = f"{model}-{version}.rom"
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM desk_firmware WHERE model = ? AND version = ?", (model, version)).fetchone():
            raise HTTPException(409, f"{model} {version} is already in the library.")
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    tmp = os.path.join(FIRMWARE_DIR, f".upload-{secrets.token_hex(8)}")
    digest, size, head = hashlib.sha256(), 0, b""
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > FIRMWARE_MAX_BYTES:
                    raise HTTPException(413, f"That's over {FIRMWARE_MAX_BYTES // (1024 * 1024)} MB: not a phone's firmware.")
                if len(head) < 16:
                    head += chunk[:16 - len(head)]
                digest.update(chunk)
                f.write(chunk)
        if head.startswith(b"PK"):
            raise HTTPException(400, "That's a zip file: unzip it and upload the .rom inside.")
        if head.lstrip().lower().startswith((b"<!doctype", b"<html", b"<?xml")):
            raise HTTPException(400, "That's a web page, not firmware: download the .rom again.")
        if size < FIRMWARE_MIN_BYTES:
            raise HTTPException(400, "That's too small to be a phone's firmware.")
        with _lock, get_db() as conn:
            if conn.execute("SELECT 1 FROM desk_firmware WHERE name = ?", (name,)).fetchone():
                raise HTTPException(409, f"{model} {version} is already in the library.")
            os.replace(tmp, os.path.join(FIRMWARE_DIR, name))
            conn.execute("INSERT INTO desk_firmware (model, version, name, size, sha256, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (model, version, name, size, digest.hexdigest(), _now()))
            conn.commit()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    changes_log.info(f"Firmware {name} added to the library ({size} bytes, sha256 {digest.hexdigest()[:12]})")
    return {"ok": True, "message": f"{model} {version} added. Choose it as the target to send it to {model} phones."}


class FirmwareTarget(BaseModel):
    firmware_id: Optional[int] = None  # None: leave that model's phones as they are


@router.put("/desk-firmware/target/{model}")
def api_firmware_target(model: str, req: FirmwareTarget):
    if model not in MODELS:
        raise HTTPException(400, "Unknown model.")
    with _lock, get_db() as conn:
        if req.firmware_id is None:
            conn.execute("DELETE FROM desk_firmware_targets WHERE model = ?", (model,))
            conn.commit()
            changes_log.info(f"Firmware target for {model}: none (phones left as they are)")
            return {"ok": True, "message": f"{model} phones are left on what they run."}
        fw = conn.execute("SELECT * FROM desk_firmware WHERE id = ?", (req.firmware_id,)).fetchone()
        if not fw or fw["model"] != model:
            raise HTTPException(400, f"That firmware isn't for the {model}.")
        conn.execute("DELETE FROM desk_firmware_attempts WHERE firmware_id = ?", (fw["id"],))  # choosing it again retries
        conn.execute("INSERT INTO desk_firmware_targets (model, firmware_id) VALUES (?, ?) "
                     "ON CONFLICT(model) DO UPDATE SET firmware_id = excluded.firmware_id", (model, fw["id"]))
        conn.commit()
    changes_log.info(f"Firmware target for {model}: {fw['version']}")
    return {"ok": True, "message": f"{model} phones on anything else upgrade to {fw['version']} at their next check "
                                   f"(within {REPEAT_MINUTES} minutes, or when they restart)."}


@router.delete("/desk-firmware/{fw_id}")
def api_delete_firmware(fw_id: int):
    with _lock, get_db() as conn:
        fw = conn.execute("SELECT * FROM desk_firmware WHERE id = ?", (fw_id,)).fetchone()
        if not fw:
            raise HTTPException(404, "Not found.")
        if conn.execute("SELECT 1 FROM desk_firmware_targets WHERE firmware_id = ?", (fw_id,)).fetchone():
            raise HTTPException(409, f"{fw['model']} {fw['version']} is the {fw['model']}'s target: choose another first.")
        conn.execute("DELETE FROM desk_firmware WHERE id = ?", (fw_id,))
        conn.execute("DELETE FROM desk_firmware_attempts WHERE firmware_id = ?", (fw_id,))
        conn.commit()
    path = os.path.join(FIRMWARE_DIR, fw["name"])
    if os.path.isfile(path):
        os.remove(path)
    changes_log.info(f"Firmware {fw['name']} removed from the library")
    return {"ok": True, "message": f"{fw['model']} {fw['version']} removed."}
