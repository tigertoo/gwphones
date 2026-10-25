"""
Phone System (Asterisk PBX) management — the "Phone System" tab.

The dashboard keeps its own small SQLite store (pbx.db) as the source of
truth for extensions, ring groups, inbound routes and trunk settings, and
GENERATES two Asterisk include files from it on every change, in a folder
of their own (PBX_GENERATED_DIR, /etc/asterisk/portal on the VPS) so the
dashboard needs no write access to the rest of /etc/asterisk:

    portal/pjsip_extensions.conf   extensions (endpoint/auth/aor)
    portal/extensions_portal.conf  [globals], ring groups, inbound
                                   routing, outbound trunk order

then reloads Asterisk (pjsip reload + dialplan reload — neither drops
calls in progress). Everything else in /etc/asterisk (transports, the
trunk definitions themselves, the static dialplan in extensions.conf) is
still edited by hand and never touched from here.

First run: if pbx.db is empty, the extensions already in
pjsip_extensions.conf (from make_extensions.py) and the [globals] values in
extensions.conf are imported, so existing phones keep their passwords.

Live status and reloads go through `asterisk -rx`, which needs the
service's user to be in the asterisk group, with Asterisk's control socket
open to that group (astctlpermissions = 0660 in asterisk.conf).
"""

import os
import re
import html
import base64
import hashlib
import pwd
import grp
import secrets
import sqlite3
import logging
import logging.handlers
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

import httpx
import segno
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ASTERISK_DIR = os.environ.get("PBX_ASTERISK_DIR", "/etc/asterisk")
PBX_DB_PATH = os.environ.get("PBX_DB", "/var/lib/admin-dashboard/pbx.db")
ASTERISK_BIN = os.environ.get("PBX_ASTERISK_BIN", "asterisk")
CHANGES_LOG_PATH = os.environ.get("PBX_CHANGES_LOG", "/var/log/admin-dashboard/pbx-changes.log")
# Shown on the "set up a phone" card. Must match transport-tls in pjsip.conf.
PUBLIC_HOST = os.environ.get("PBX_PUBLIC_HOST", "")
PUBLIC_PORT = os.environ.get("PBX_PUBLIC_PORT", "15061")
# Public address of the QR-code setup links (see "Phone setup by QR code"
# in the README), e.g. https://amapforyourmoney.com. Empty turns them off.
PROVISION_BASE_URL = os.environ.get("PBX_PROVISION_BASE_URL", "").rstrip("/")
PROVISION_TTL = timedelta(minutes=15)
# Emailed links last longer: people don't read email within 15 minutes.
EMAIL_LINK_TTL = timedelta(hours=int(os.environ.get("PBX_EMAIL_LINK_HOURS", "72")))

# Setup emails go out through Microsoft Graph, with the same settings as
# Money Mapper (an app registration allowed to send as EMAIL_FROM_ADDRESS).
MS_GRAPH_TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "")
MS_GRAPH_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "")
MS_GRAPH_CLIENT_SECRET = os.environ.get("MS_GRAPH_CLIENT_SECRET", "")
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS", "")
EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
# Groundwire may fetch the settings more than once (a retry, HEAD then GET).
PROVISION_GRACE = timedelta(seconds=60)

# Where the two generated files go. Defaults to ASTERISK_DIR itself, as
# before; on the VPS it's /etc/asterisk/portal (owned by the dashboard's
# user, group asterisk), included from extensions.conf and pjsip.conf.
GENERATED_DIR = os.environ.get("PBX_GENERATED_DIR", ASTERISK_DIR)
EXT_CONF = os.path.join(GENERATED_DIR, "pjsip_extensions.conf")
PORTAL_DIALPLAN = os.path.join(GENERATED_DIR, "extensions_portal.conf")
# Mailboxes, included from voicemail.conf. They live in their own voicemail
# context so they can't clash with anything defined there by hand.
VOICEMAIL_CONF = os.path.join(GENERATED_DIR, "voicemail_portal.conf")
VM_CONTEXT = "portal"
VOICEMAIL_NUMBER = "*97"  # check your own voicemail (static dialplan)
STATIC_DIALPLAN = os.path.join(ASTERISK_DIR, "extensions.conf")

# Must line up with the static dialplan: _2XX dials extensions, and ring
# groups live in their own 6XX range so the two can never collide.
EXT_MIN, EXT_MAX = 200, 299
GROUP_MIN, GROUP_MAX = 600, 699

# The trunks themselves (host, IP matching) are defined by hand in
# pjsip.conf. Here we only decide whether/in what order they're used for
# outbound calls, and how each one wants the number written:
#   ARG1 = national format (0871234567), ARG2 = international (353871234567)
TRUNKS = {
    "irishvoip": {"label": "Irish VoIP", "host": "connect.irishvoip.com", "dial": "${ARG1}"},
    "voxbeam": {"label": "Voxbeam", "host": "sbc.voxbeam.com", "dial": "${VOXBEAM_PREFIX}${ARG2}"},
}
VOXBEAM_ROUTES = {"0011101": "Gold", "0011102": "Platinum", "0011103": "Silver", "0011104": "Call Center"}

# Which trunk(s) each kind of number goes out on. The static dialplan sorts
# dialled numbers into these and calls [sub-outbound] at the matching
# extension, e.g. Gosub(sub-outbound,mobile,1(...)).
#   default = the trunk order above, with fallback; a trunk key = only that
#   trunk; off = refused.
NUMBER_TYPES = {
    "mobile": "Irish mobiles (083, 085, 086, 087, 089)",
    "landline": "Irish landlines and other Irish numbers (01, 021, 0818...)",
    "freephone": "Irish freephone (1800)",
    "international": "International (00 or +, outside Ireland)",
}
ROUTE_CHOICES = ["default", *TRUNKS, "off"]

DEFAULT_SETTINGS = {
    "outbound_cid": "",
    "max_outbound": "4",
    "voxbeam_prefix": "0011101",
    "trunk_order": "irishvoip,voxbeam",
    "trunk_irishvoip_enabled": "1",
    "trunk_voxbeam_enabled": "1",
    "inbound_default": "201",
    "route_mobile": "irishvoip",
    "route_landline": "default",
    "route_freephone": "irishvoip",
    "route_international": "voxbeam",
}

NAME_RE = re.compile(r"^[A-Za-z0-9 .'_-]{1,40}$")
CID_RE = re.compile(r"^\+?\d{6,15}$")

_lock = threading.Lock()
_last_apply: dict = {"at": None, "ok": None, "messages": []}

router = APIRouter(prefix="/api/pbx")
# The only routes here reachable without the dashboard's login: a phone
# scanning a setup QR code isn't on Tailscale. Outside /api/, so
# require_auth leaves them alone; each needs a valid one-time token.
public_router = APIRouter(prefix="/prov")


def _changes_logger() -> logging.Logger:
    logger = logging.getLogger("pbx_changes")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            os.makedirs(os.path.dirname(CHANGES_LOG_PATH), exist_ok=True)
            h = logging.handlers.TimedRotatingFileHandler(CHANGES_LOG_PATH, when="midnight", backupCount=90, utc=True)
            h.setFormatter(logging.Formatter("%(asctime)s UTC %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(h)
        except Exception as e:
            print(f"⚠️ Could not set up PBX change log at {CHANGES_LOG_PATH}: {e}")
    return logger


changes_log = _changes_logger()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(PBX_DB_PATH) or ".", exist_ok=True)
    fresh = not os.path.exists(PBX_DB_PATH)
    conn = sqlite3.connect(PBX_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS extensions (
               ext TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               password TEXT NOT NULL,
               enabled INTEGER NOT NULL DEFAULT 1,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL);
           CREATE TABLE IF NOT EXISTS ring_groups (
               number TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               strategy TEXT NOT NULL DEFAULT 'ringall',
               ring_time INTEGER NOT NULL DEFAULT 25,
               members TEXT NOT NULL DEFAULT '');
           CREATE TABLE IF NOT EXISTS inbound_routes (
               did TEXT PRIMARY KEY,
               target TEXT NOT NULL,
               label TEXT NOT NULL DEFAULT '');
           CREATE TABLE IF NOT EXISTS settings (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL);
           CREATE TABLE IF NOT EXISTS provision_tokens (
               token_hash TEXT PRIMARY KEY,
               ext TEXT NOT NULL,
               expires_at TEXT NOT NULL,
               used_at TEXT);"""
    )
    if fresh:
        os.chmod(PBX_DB_PATH, 0o600)  # holds SIP passwords
    for column in ("extensions ADD COLUMN vm_pin TEXT",
                   "ring_groups ADD COLUMN voicemail TEXT NOT NULL DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE {column}")
        except sqlite3.OperationalError:
            pass  # already there
    if conn.execute("SELECT 1 FROM settings WHERE key = 'imported'").fetchone() is None:
        _import_existing(conn)
    missing = conn.execute("SELECT ext FROM extensions WHERE vm_pin IS NULL").fetchall()
    for r in missing:
        conn.execute("UPDATE extensions SET vm_pin = ? WHERE ext = ?", (_new_pin(), r["ext"]))
    if missing:
        conn.commit()
    return conn


def _new_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(6))


def get_settings(conn) -> Dict[str, str]:
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return s


def _set_setting(conn, key: str, value: str):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


def _parse_conf_sections(path: str) -> List[dict]:
    """Minimal Asterisk .conf reader: [name](template) headers and key=value lines."""
    sections, current = [], None
    try:
        with open(path) as f:
            for raw in f:
                line = raw.split(";", 1)[0].strip()
                if not line:
                    continue
                m = re.match(r"^\[([^\]]+)\](?:\(([^)]*)\))?", line)
                if m:
                    current = {"name": m.group(1), "template": m.group(2) or "", "values": {}}
                    sections.append(current)
                elif current is not None and "=" in line:
                    k, v = line.split("=", 1)
                    current["values"][k.strip().rstrip(">").strip()] = v.strip().lstrip(">").strip()
    except OSError:
        pass
    return sections


def _import_existing(conn):
    """One-off: adopt what make_extensions.py and the hand-written
    [globals] already set up, so nothing on the phones has to change."""
    found = {}
    for sec in _parse_conf_sections(EXT_CONF):
        ext = sec["name"]
        if not ext.isdigit():
            continue
        entry = found.setdefault(ext, {"name": f"Ext {ext}", "password": None})
        if "password" in sec["values"]:
            entry["password"] = sec["values"]["password"]
        cid = sec["values"].get("callerid")
        if cid:
            m = re.match(r'^"([^"]*)"', cid)
            if m and NAME_RE.match(m.group(1)):
                entry["name"] = m.group(1)

    now = _now()
    for ext, e in sorted(found.items()):
        if e["password"]:
            conn.execute("INSERT OR IGNORE INTO extensions (ext, name, password, enabled, created_at, updated_at) "
                         "VALUES (?, ?, ?, 1, ?, ?)", (ext, e["name"], e["password"], now, now))

    for sec in _parse_conf_sections(STATIC_DIALPLAN):
        if sec["name"] != "globals":
            continue
        v = sec["values"]
        if CID_RE.match(v.get("OUTBOUND_CID", "")):
            _set_setting(conn, "outbound_cid", v["OUTBOUND_CID"])
        if v.get("MAX_OUTBOUND", "").isdigit():
            _set_setting(conn, "max_outbound", v["MAX_OUTBOUND"])
        if v.get("VOXBEAM_PREFIX") in VOXBEAM_ROUTES:
            _set_setting(conn, "voxbeam_prefix", v["VOXBEAM_PREFIX"])
        if v.get("INBOUND_EXT", "").isdigit():
            _set_setting(conn, "inbound_default", v["INBOUND_EXT"])

    _set_setting(conn, "imported", "1")
    conn.commit()
    changes_log.info(f"Imported {len(found)} existing extension(s) into pbx.db")


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def _members(row) -> List[str]:
    return [m for m in (row["members"] or "").split(",") if m]


def build_extensions_conf(conn) -> str:
    out = ["; Generated by the admin dashboard (Phone System tab).",
           "; Do not edit by hand: changes here are overwritten on the next save.", ""]
    for r in conn.execute("SELECT * FROM extensions WHERE enabled = 1 ORDER BY ext"):
        out += [f"[{r['ext']}](endpoint-ext)",
                f"auth={r['ext']}",
                f"aors={r['ext']}",
                f"callerid=\"{r['name']}\" <{r['ext']}>",
                f"mailboxes={r['ext']}@{VM_CONTEXT}",
                "",
                f"[{r['ext']}](auth-ext)",
                f"username={r['ext']}",
                f"password={r['password']}",
                "",
                f"[{r['ext']}](aor-ext)",
                ""]
    return "\n".join(out)


def _outbound_block(s: Dict[str, str], exten: str, trunks: List[str], refused: bool = False) -> List[str]:
    out = [f"exten => {exten},1,Set(GROUP(outbound)=calls)",
           " same => n,GotoIf($[${GROUP_COUNT(calls@outbound)} > ${MAX_OUTBOUND}]?busy)"]
    if s["outbound_cid"]:
        out.append(" same => n,Set(CALLERID(num)=${OUTBOUND_CID})")
    if refused:
        out += [" same => n,NoOp(These numbers are blocked)", " same => n,Goto(busy)"]
    elif not trunks:
        out += [" same => n,NoOp(No enabled trunk for these numbers)", " same => n,Goto(busy)"]
    for i, t in enumerate(trunks):
        out.append(f" same => n,Dial(PJSIP/{TRUNKS[t]['dial']}@{t},60)")
        if i < len(trunks) - 1:
            # Only fall through to the next trunk if this one couldn't take
            # the call at all (down, refused, congested), never after a
            # normal answer/busy/no-answer.
            out.append(' same => n,GotoIf($["${DIALSTATUS}" != "CHANUNAVAIL" & "${DIALSTATUS}" != "CONGESTION"]?done)')
    return out + [" same => n(done),Hangup()",
                  " same => n(busy),Congestion(5)",
                  " same => n,Hangup()"]


def build_voicemail_conf(conn) -> str:
    out = ["; Generated by the admin dashboard (Phone System tab).",
           "; Do not edit by hand: changes here are overwritten on the next save.",
           "; Included from voicemail.conf. mailbox => PIN,name", "",
           f"[{VM_CONTEXT}]"]
    for r in conn.execute("SELECT * FROM extensions WHERE enabled = 1 ORDER BY ext"):
        out.append(f"{r['ext']} => {r['vm_pin']},{r['name']}")
    return "\n".join(out) + "\n"


def build_portal_dialplan(conn) -> str:
    s = get_settings(conn)
    active = {r["ext"] for r in conn.execute("SELECT ext FROM extensions WHERE enabled = 1")}

    out = ["; Generated by the admin dashboard (Phone System tab).",
           "; Do not edit by hand: changes here are overwritten on the next save.",
           "; Included from extensions.conf.", "",
           "[globals]",
           f"OUTBOUND_CID={s['outbound_cid']}",
           f"MAX_OUTBOUND={s['max_outbound']}",
           f"VOXBEAM_PREFIX={s['voxbeam_prefix']}",
           "",
           "; ---- Ring groups (included into from-internal) ----",
           "[portal-ringgroups]"]

    for g in conn.execute("SELECT * FROM ring_groups ORDER BY number"):
        members = [m for m in _members(g) if m in active]
        n, t = g["number"], g["ring_time"]
        out.append(f"exten => {n},1,NoOp(Ring group {n}: {g['name']})")
        if not members:
            out.append(" same => n,NoOp(No active members)")
        elif g["strategy"] == "hunt":
            for m in members:
                out.append(f" same => n,Dial(PJSIP/{m},{t})")
                out.append(' same => n,ExecIf($["${DIALSTATUS}" = "ANSWER"]?Hangup())')
        else:
            out.append(f" same => n,Dial({'&'.join(f'PJSIP/{m}' for m in members)},{t})")
            out.append(' same => n,ExecIf($["${DIALSTATUS}" = "ANSWER"]?Hangup())')
        if g["voicemail"] in active:
            out.append(f" same => n,VoiceMail({g['voicemail']}@{VM_CONTEXT},u)")
        out.append(" same => n,Hangup()")

    enabled = [t for t in s["trunk_order"].split(",") if t in TRUNKS and s.get(f"trunk_{t}_enabled") == "1"]
    out += ["", "; ---- Outbound: one extension per kind of number ----",
            "; ARG1 = national number, ARG2 = international (353..., 44...)",
            "; s = the trunk order (kept for dialplans that don't say the kind)",
            "[sub-outbound]"]
    out += _outbound_block(s, "s", enabled)
    for kind in NUMBER_TYPES:
        route = s.get(f"route_{kind}", "default")
        trunks = enabled if route == "default" else [t for t in enabled if t == route]
        out += [""] + _outbound_block(s, kind, trunks, refused=route == "off")
    out += ["",
            "; ---- Inbound: DID-specific routes, then the default ----",
            "[portal-inbound]"]
    for r in conn.execute("SELECT * FROM inbound_routes ORDER BY did"):
        out.append(f"exten => {r['did']},1,Goto(from-internal,{r['target']},1)")
    default = s["inbound_default"]
    out.append(f"exten => _X.,1,Goto(from-internal,{default},1)")
    out.append(f"exten => s,1,Goto(from-internal,{default},1)")
    out.append("")
    return "\n".join(out)


def _write_conf(path: str, content: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    # Asterisk only needs to read these. As root the files are handed to
    # the asterisk user; otherwise just to its group, which the service's
    # user belongs to (the folder's setgid bit does the same).
    try:
        uid, gid = pwd.getpwnam("asterisk").pw_uid, grp.getgrnam("asterisk").gr_gid
        try:
            os.chown(tmp, uid, gid)
        except PermissionError:
            os.chown(tmp, -1, gid)
    except (KeyError, PermissionError):
        pass  # local dev: no asterisk user
    os.chmod(tmp, 0o640)
    if os.path.exists(path):
        try:
            with open(path) as old, open(path + ".bak", "w") as bak:
                bak.write(old.read())
            os.chmod(path + ".bak", 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def asterisk_cli(command: str, timeout: int = 8) -> Optional[str]:
    """Runs `asterisk -rx`. Returns output, or None if Asterisk isn't reachable."""
    try:
        res = subprocess.run([ASTERISK_BIN, "-rx", command], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0 or "Unable to connect to remote asterisk" in out:
        return None
    return out


def apply_config(conn, reason: str) -> dict:
    """Regenerate both include files and reload Asterisk."""
    messages, ok = [], True
    try:
        _write_conf(EXT_CONF, build_extensions_conf(conn))
        _write_conf(PORTAL_DIALPLAN, build_portal_dialplan(conn))
        _write_conf(VOICEMAIL_CONF, build_voicemail_conf(conn))
    except OSError as e:
        ok = False
        messages.append(f"Saved, but couldn't write the Asterisk config files: {e}")
    if ok:
        for cmd in ("pjsip reload", "dialplan reload"):
            if asterisk_cli(cmd) is None:
                ok = False
                messages.append(f"Saved, but '{cmd}' failed. Is Asterisk running?")
    if ok:
        messages.append("Saved and applied.")
        # Not fatal: calls work without it, they just can't leave messages.
        out = asterisk_cli("voicemail reload")
        if out is None or "No such command" in out:
            messages.append("Voicemail didn't reload: is app_voicemail installed (apt install asterisk-voicemail)?")
    _last_apply.update({"at": _now(), "ok": ok, "messages": messages})
    changes_log.info(f"{reason} -> {'applied' if ok else 'NOT applied: ' + '; '.join(messages)}")
    return {"ok": ok, "messages": messages}


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------

CONTACT_STATUSES = {"Avail", "Unavail", "NonQual", "Unknown", "Created", "Removed", "Updated", "Reachable", "Unreachable"}


def parse_contacts(output: str) -> Dict[str, dict]:
    contacts = {}
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) < 3 or tokens[0] != "Contact:" or tokens[1].startswith("<"):
            continue
        aor, _, uri = tokens[1].partition("/")
        status, rtt = None, None
        for i, tok in enumerate(tokens[2:], start=2):
            if tok in CONTACT_STATUSES:
                status = tok
                if i + 1 < len(tokens):
                    try:
                        value = float(tokens[i + 1])
                        if value == value:  # "nan" when unreachable; NaN isn't valid JSON
                            rtt = round(value, 1)
                    except ValueError:
                        pass
                break
        ip = re.search(r"@(\[[^\]]+\]|[^:;>]+)", uri)
        contacts[aor] = {"status": status or "Unknown", "rtt_ms": rtt,
                         "address": ip.group(1).strip("[]") if ip else None}
    return contacts


def parse_channels(output: str) -> List[dict]:
    channels = []
    for line in output.splitlines():
        f = line.split("!")
        if len(f) < 12 or f[0].startswith("Local/"):
            continue
        try:
            duration = int(f[11])
        except ValueError:
            duration = 0
        channels.append({"channel": f[0], "context": f[1], "exten": f[2], "state": f[4],
                         "application": f[5], "caller_id": f[7], "duration_seconds": duration})
    return channels


def get_status() -> dict:
    version = asterisk_cli("core show version", timeout=4)
    if version is None:
        return {"asterisk_running": False, "checked_at": _now(), "extensions": {}, "trunks": {},
                "channels": [], "last_apply": _last_apply}
    contacts = parse_contacts(asterisk_cli("pjsip show contacts") or "")
    return {
        "asterisk_running": True,
        "version": version.strip().splitlines()[0] if version.strip() else "",
        "checked_at": _now(),
        "extensions": {k: v for k, v in contacts.items() if k.isdigit()},
        "trunks": {k: contacts.get(k, {"status": "Unknown", "rtt_ms": None, "address": None}) for k in TRUNKS},
        "channels": parse_channels(asterisk_cli("core show channels concise") or ""),
        "last_apply": _last_apply,
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise HTTPException(400, "Names can be 1-40 characters: letters, numbers, spaces and . ' _ -")
    return name


def _in_range(value: str, lo: int, hi: int, what: str) -> str:
    value = (value or "").strip()
    # ASCII digits only: str.isdigit() also accepts "٢٠١" and "²", which
    # would end up as section names in the Asterisk config.
    if not re.fullmatch(r"[0-9]+", value) or not lo <= int(value) <= hi:
        raise HTTPException(400, f"{what} must be a number from {lo} to {hi}.")
    return value


def normalize_did(did: str) -> str:
    """Same normalisation as the static from-trunk context: digits only,
    00/+ international prefix stripped, national 0 replaced by 353."""
    d = re.sub(r"[\s()-]", "", did or "")
    if d.startswith("+"):
        d = d[1:]
    elif d.startswith("00"):
        d = d[2:]
    elif d.startswith("0"):
        d = "353" + d[1:]
    if not re.match(r"^\d{6,15}$", d):
        raise HTTPException(400, "Enter the number as dialled, e.g. 01 234 5678 or +353 1 234 5678.")
    return d


def _valid_target(conn, target: str) -> str:
    target = (target or "").strip()
    if conn.execute("SELECT 1 FROM extensions WHERE ext = ?", (target,)).fetchone() or \
       conn.execute("SELECT 1 FROM ring_groups WHERE number = ?", (target,)).fetchone():
        return target
    raise HTTPException(400, f"{target or 'That'} isn't an existing extension or ring group.")


def _used_as_target(conn, number: str) -> Optional[str]:
    if get_settings(conn)["inbound_default"] == number:
        return "the default destination for inbound calls"
    r = conn.execute("SELECT did FROM inbound_routes WHERE target = ?", (number,)).fetchone()
    return f"the destination for inbound number {r['did']}" if r else None


# ---------------------------------------------------------------------------
# Routes — auth is applied by main.py (include_router dependencies)
# ---------------------------------------------------------------------------

@router.get("/status")
def api_status():
    return get_status()


@router.get("/config")
def api_config():
    with get_db() as conn:
        s = get_settings(conn)
        return {
            "extensions": [{"ext": r["ext"], "name": r["name"], "enabled": bool(r["enabled"]),
                            "created_at": r["created_at"], "updated_at": r["updated_at"]}
                           for r in conn.execute("SELECT * FROM extensions ORDER BY ext")],
            "ring_groups": [{"number": r["number"], "name": r["name"], "strategy": r["strategy"],
                             "ring_time": r["ring_time"], "members": _members(r), "voicemail": r["voicemail"]}
                            for r in conn.execute("SELECT * FROM ring_groups ORDER BY number")],
            "inbound_routes": [dict(r) for r in conn.execute("SELECT * FROM inbound_routes ORDER BY did")],
            "settings": {
                "outbound_cid": s["outbound_cid"],
                "max_outbound": int(s["max_outbound"]),
                "voxbeam_prefix": s["voxbeam_prefix"],
                "trunk_order": [t for t in s["trunk_order"].split(",") if t in TRUNKS],
                "trunks_enabled": {t: s.get(f"trunk_{t}_enabled") == "1" for t in TRUNKS},
                "inbound_default": s["inbound_default"],
                "routes": {k: s.get(f"route_{k}", "default") for k in NUMBER_TYPES},
            },
            "trunks": {k: {"label": v["label"], "host": v["host"]} for k, v in TRUNKS.items()},
            "number_types": NUMBER_TYPES,
            "voxbeam_routes": VOXBEAM_ROUTES,
            "limits": {"ext_min": EXT_MIN, "ext_max": EXT_MAX, "group_min": GROUP_MIN, "group_max": GROUP_MAX},
        }


class ExtensionCreate(BaseModel):
    ext: Optional[str] = None  # next free number if omitted
    name: str


class ExtensionUpdate(BaseModel):
    name: str
    enabled: bool


@router.post("/extensions")
def api_create_extension(req: ExtensionCreate):
    name = _clean_name(req.name)
    with _lock, get_db() as conn:
        taken = {r["ext"] for r in conn.execute("SELECT ext FROM extensions")}
        if req.ext:
            ext = _in_range(req.ext, EXT_MIN, EXT_MAX, "Extension")
            if ext in taken:
                raise HTTPException(409, f"Extension {ext} already exists.")
        else:
            free = [str(n) for n in range(EXT_MIN + 1, EXT_MAX + 1) if str(n) not in taken]
            if not free:
                raise HTTPException(409, "No free extension numbers left.")
            ext = free[0]
        now = _now()
        conn.execute("INSERT INTO extensions (ext, name, password, vm_pin, enabled, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, 1, ?, ?)", (ext, name, secrets.token_urlsafe(24), _new_pin(), now, now))
        conn.commit()
        result = apply_config(conn, f"Created extension {ext} ({name})")
    return {"ext": ext, **result}


@router.put("/extensions/{ext}")
def api_update_extension(ext: str, req: ExtensionUpdate):
    name = _clean_name(req.name)
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET name = ?, enabled = ?, updated_at = ? WHERE ext = ?",
                        (name, int(req.enabled), _now(), ext)).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        if not req.enabled:
            _revoke_setup_links(conn, ext)
        conn.commit()
        return apply_config(conn, f"Updated extension {ext}: name={name}, enabled={req.enabled}")


@router.post("/extensions/{ext}/reset-password")
def api_reset_password(ext: str):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET password = ?, updated_at = ? WHERE ext = ?",
                        (secrets.token_urlsafe(24), _now(), ext)).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        _revoke_setup_links(conn, ext)
        conn.commit()
        return apply_config(conn, f"Reset password for extension {ext}")


@router.post("/extensions/{ext}/reset-voicemail-pin")
def api_reset_voicemail_pin(ext: str):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET vm_pin = ?, updated_at = ? WHERE ext = ?",
                        (_new_pin(), _now(), ext)).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"New voicemail PIN for extension {ext}")


@router.get("/extensions/{ext}/credentials")
def api_credentials(ext: str):
    with get_db() as conn:
        r = conn.execute("SELECT ext, password, vm_pin FROM extensions WHERE ext = ?", (ext,)).fetchone()
    if not r:
        raise HTTPException(404, "Extension not found.")
    changes_log.info(f"Viewed credentials for extension {ext}")
    return {"username": r["ext"], "password": r["password"], "server": PUBLIC_HOST,
            "port": PUBLIC_PORT, "transport": "TLS", "media_encryption": "SRTP (SDES)",
            "voicemail_number": VOICEMAIL_NUMBER, "voicemail_pin": r["vm_pin"]}


# ---------------------------------------------------------------------------
# Phone setup by QR code (Groundwire / Acrobits, and Linphone)
#
# The dashboard makes a one-time link and shows it as a QR code, or emails
# it (a link to tap on the phone, plus QR codes for reading on a computer).
# - Groundwire: the phone's Camera opens it in Safari, which hands it to
#   Groundwire (provlinkbs://), and Groundwire fetches account.xml.
# - Linphone: the QR code is the settings link itself (linphone.xml),
#   scanned from inside Linphone, which fetches it directly. Emailed, the
#   link opens a page with that address to paste into Linphone.
# Links last 15 minutes (emailed ones 72 hours, PBX_EMAIL_LINK_HOURS) and
# stop working a minute after the settings are first fetched; a new
# password, or disabling or deleting the extension, cancels them. Only a
# hash of each token is stored.
# ---------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _revoke_setup_links(conn, ext: str):
    conn.execute("DELETE FROM provision_tokens WHERE ext = ?", (ext,))


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


SETUP_APPS = {"groundwire": "", "linphone": "/linphone.xml"}  # what the QR code points at
APP_LABELS = {"groundwire": "Groundwire", "linphone": "Linphone"}


def _setup_ready():
    if not PROVISION_BASE_URL or not PUBLIC_HOST:
        raise HTTPException(400, "Set PBX_PROVISION_BASE_URL and PBX_PUBLIC_HOST in the dashboard's .env to use QR setup.")


def _make_token(ext: str, ttl: timedelta):
    """A new one-time setup token for an enabled extension: (token, row, expires)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with _lock, get_db() as conn:
        e = conn.execute("SELECT * FROM extensions WHERE ext = ? AND enabled = 1", (ext,)).fetchone()
        if not e:
            raise HTTPException(404, "Extension not found, or turned off.")
        conn.execute("DELETE FROM provision_tokens WHERE expires_at < ?", (now.isoformat(),))
        conn.execute("INSERT INTO provision_tokens (token_hash, ext, expires_at) VALUES (?, ?, ?)",
                     (_token_hash(token), ext, (now + ttl).isoformat()))
        conn.commit()
    return token, e, now + ttl


def _setup_url(token: str, app: str) -> str:
    return f"{PROVISION_BASE_URL}/prov/{token}{SETUP_APPS[app]}"


def _local_time(when: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo(os.environ.get("PBX_TIMEZONE", "Europe/Dublin")))
    except Exception:
        pass
    return when.strftime("%-d %b %Y, %H:%M")


class SetupLinkRequest(BaseModel):
    app: str = "groundwire"


@router.post("/extensions/{ext}/setup-link")
def api_setup_link(ext: str, req: Optional[SetupLinkRequest] = None):
    app = (req or SetupLinkRequest()).app
    if app not in SETUP_APPS:
        raise HTTPException(400, "Unknown phone app.")
    _setup_ready()
    token, _, expires = _make_token(ext, PROVISION_TTL)
    changes_log.info(f"Made a {app} QR setup link for extension {ext}")
    url = _setup_url(token, app)
    return {"url": url, "expires_at": expires.isoformat(),
            "qr": segno.make(url, error="m").svg_data_uri(scale=6, border=2)}


def _live_token(conn, token: str):
    """The token's row if it can still be used, else None."""
    r = conn.execute("SELECT * FROM provision_tokens WHERE token_hash = ?", (_token_hash(token),)).fetchone()
    now = datetime.now(timezone.utc)
    if not r or datetime.fromisoformat(r["expires_at"]) < now:
        return None
    if r["used_at"] and datetime.fromisoformat(r["used_at"]) + PROVISION_GRACE < now:
        return None
    return r


_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
_GONE = "This setup link has expired or has already been used. Ask for a new one."


def _setup_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phone setup</title>
<style>body{{font:17px -apple-system,system-ui,sans-serif;margin:40px 20px;text-align:center;color:#222}}
a.btn,button.btn{{display:inline-block;padding:14px 22px;border:0;border-radius:10px;background:#0a7cff;color:#fff;
text-decoration:none;font:600 17px -apple-system,system-ui,sans-serif}}
p{{max-width:420px;margin:16px auto}} code{{word-break:break-all;font-size:13px}}</style></head>
<body><h1>{title}</h1>
{body}
</body></html>""", headers=_NO_STORE)


def _linphone_page(token: str, r) -> HTMLResponse:
    # Linphone has no paste-a-link option, only Scan QR code. Some versions
    # open linphone-config: links (two spellings; which works isn't
    # documented), so offer both and keep the QR code as the sure way.
    url = _setup_url(token, "linphone")
    link = html.escape(f"linphone-config:{url}")
    alt = html.escape(f"linphone-config://{url.split('://', 1)[-1]}")
    return _setup_page(f"Set up extension {html.escape(r['ext'])} in Linphone", f"""
<p>Linphone must be installed on this phone first.</p>
<p><a class="btn" href="{link}">Open in Linphone</a></p>
<p><small>Nothing happened? <a href="{alt}">Try this link instead</a>.</small></p>
<p>If neither works, open the setup email on a computer, then in Linphone tap <b>Scan QR code</b>
and scan the code from the screen.</p>
<p><small>This link works once and expires {_local_time(datetime.fromisoformat(r["expires_at"]))}.</small></p>""")


@public_router.get("/{token}", response_class=HTMLResponse)
def prov_page(token: str, app: str = "groundwire"):
    """What Safari opens from the QR code or the emailed link. Only hands
    over to the app; the settings themselves come from account.xml or
    linphone.xml, so a visit here (or a link preview) doesn't use the
    token up."""
    with get_db() as conn:
        r = _live_token(conn, token)
    if not r:
        return HTMLResponse(f"<p>{_GONE}</p>", status_code=404, headers=_NO_STORE)
    if app == "linphone":
        return _linphone_page(token, r)
    host_path = f"{PROVISION_BASE_URL.split('://', 1)[-1]}/prov/{token}/account.xml"
    link = html.escape(f"provlinkbs://{host_path}")
    return _setup_page(f"Set up extension {html.escape(r['ext'])}", f"""
<p>Groundwire must be installed on this phone first.</p>
<p><a class="btn" href="{link}">Open in Groundwire</a></p>
<p><small>This link works once and expires {_local_time(datetime.fromisoformat(r["expires_at"]))}.</small></p>""")


def _redeem(token: str, request: Request, app: str):
    """The extension the token is for, marking the token used; None if it
    can't be used any more."""
    with _lock, get_db() as conn:
        r = _live_token(conn, token)
        e = r and conn.execute("SELECT * FROM extensions WHERE ext = ? AND enabled = 1", (r["ext"],)).fetchone()
        if not e:
            return None
        if not r["used_at"]:
            conn.execute("UPDATE provision_tokens SET used_at = ? WHERE token_hash = ?", (_now(), r["token_hash"]))
            conn.commit()
    changes_log.info(f"Phone fetched {app} setup for extension {e['ext']} from {_client_ip(request)} "
                     f"({request.headers.get('user-agent', 'no user agent')[:120]})")
    return e


def _gone():
    return Response(_GONE, status_code=404, media_type="text/plain", headers=_NO_STORE)


x = lambda v: xml_escape(str(v))


@public_router.get("/{token}/account.xml")
def prov_account(token: str, request: Request):
    """The account settings, in Acrobits' Account XML format."""
    e = _redeem(token, request, "Groundwire")
    if not e:
        return _gone()
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<account>
  <title>{x(e["name"])} ({x(e["ext"])})</title>
  <username>{x(e["ext"])}</username>
  <password>{x(e["password"])}</password>
  <userDisplayName>{x(e["name"])}</userDisplayName>
  <host>{x(PUBLIC_HOST)}:{x(PUBLIC_PORT)}</host>
  <transport>tls</transport>
  <sdesIncoming>required</sdesIncoming>
  <sdesOutgoing>required</sdesOutgoing>
  <icm>push</icm>
  <voiceMailNumber>{VOICEMAIL_NUMBER}</voiceMailNumber>
</account>
"""
    return Response(body, media_type="application/xml", headers=_NO_STORE)


@router.delete("/extensions/{ext}")
def api_delete_extension(ext: str):
    with _lock, get_db() as conn:
        if not conn.execute("SELECT 1 FROM extensions WHERE ext = ?", (ext,)).fetchone():
            raise HTTPException(404, "Extension not found.")
        used = _used_as_target(conn, ext)
        if used:
            raise HTTPException(409, f"Extension {ext} is {used}. Change that first.")
        for g in conn.execute("SELECT * FROM ring_groups").fetchall():
            if ext in _members(g):
                conn.execute("UPDATE ring_groups SET members = ? WHERE number = ?",
                             (",".join(m for m in _members(g) if m != ext), g["number"]))
        conn.execute("DELETE FROM extensions WHERE ext = ?", (ext,))
        conn.execute("UPDATE ring_groups SET voicemail = '' WHERE voicemail = ?", (ext,))
        _revoke_setup_links(conn, ext)
        conn.commit()
        return apply_config(conn, f"Deleted extension {ext}")


class RingGroupRequest(BaseModel):
    number: Optional[str] = None  # next free number if omitted (create only)
    name: str
    strategy: str = "ringall"
    ring_time: int = 25
    members: List[str] = []
    voicemail: str = ""  # mailbox (extension) for unanswered calls, or none


def _clean_group(conn, req: RingGroupRequest) -> dict:
    if req.strategy not in ("ringall", "hunt"):
        raise HTTPException(400, "Strategy must be 'ringall' or 'hunt'.")
    if not 5 <= req.ring_time <= 120:
        raise HTTPException(400, "Ring time must be 5-120 seconds.")
    exts = {r["ext"] for r in conn.execute("SELECT ext FROM extensions")}
    members = list(dict.fromkeys(m.strip() for m in req.members if m.strip()))
    bad = [m for m in members if m not in exts]
    if bad:
        raise HTTPException(400, f"Not existing extensions: {', '.join(bad)}")
    if not members:
        raise HTTPException(400, "Add at least one extension to the group.")
    voicemail = req.voicemail.strip()
    if voicemail and voicemail not in exts:
        raise HTTPException(400, f"Voicemail box {voicemail} isn't an existing extension.")
    return {"name": _clean_name(req.name), "strategy": req.strategy,
            "ring_time": req.ring_time, "members": ",".join(members), "voicemail": voicemail}


@router.post("/ringgroups")
def api_create_group(req: RingGroupRequest):
    with _lock, get_db() as conn:
        g = _clean_group(conn, req)
        taken = {r["number"] for r in conn.execute("SELECT number FROM ring_groups")}
        if req.number:
            number = _in_range(req.number, GROUP_MIN, GROUP_MAX, "Ring group number")
            if number in taken:
                raise HTTPException(409, f"Ring group {number} already exists.")
        else:
            free = [str(n) for n in range(GROUP_MIN, GROUP_MAX + 1) if str(n) not in taken]
            if not free:
                raise HTTPException(409, "No free ring group numbers left.")
            number = free[0]
        conn.execute("INSERT INTO ring_groups (number, name, strategy, ring_time, members, voicemail) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (number, g["name"], g["strategy"], g["ring_time"], g["members"], g["voicemail"]))
        conn.commit()
        result = apply_config(conn, f"Created ring group {number} ({g['name']}): {g['strategy']} {g['members']}, "
                                    f"voicemail={g['voicemail'] or 'none'}")
    return {"number": number, **result}


@router.put("/ringgroups/{number}")
def api_update_group(number: str, req: RingGroupRequest):
    with _lock, get_db() as conn:
        g = _clean_group(conn, req)
        if conn.execute("UPDATE ring_groups SET name = ?, strategy = ?, ring_time = ?, members = ?, voicemail = ? "
                        "WHERE number = ?",
                        (g["name"], g["strategy"], g["ring_time"], g["members"], g["voicemail"], number)).rowcount == 0:
            raise HTTPException(404, "Ring group not found.")
        conn.commit()
        return apply_config(conn, f"Updated ring group {number}: {g['strategy']} {g['members']}, "
                                  f"voicemail={g['voicemail'] or 'none'}")


@router.delete("/ringgroups/{number}")
def api_delete_group(number: str):
    with _lock, get_db() as conn:
        used = _used_as_target(conn, number)
        if used:
            raise HTTPException(409, f"Ring group {number} is {used}. Change that first.")
        if conn.execute("DELETE FROM ring_groups WHERE number = ?", (number,)).rowcount == 0:
            raise HTTPException(404, "Ring group not found.")
        conn.commit()
        return apply_config(conn, f"Deleted ring group {number}")


class InboundRouteRequest(BaseModel):
    did: str
    target: str
    label: str = ""


@router.post("/inbound")
def api_set_inbound_route(req: InboundRouteRequest):
    did = normalize_did(req.did)
    label = req.label.strip()
    if label:
        label = _clean_name(label)
    with _lock, get_db() as conn:
        target = _valid_target(conn, req.target)
        conn.execute("INSERT INTO inbound_routes (did, target, label) VALUES (?, ?, ?) "
                     "ON CONFLICT(did) DO UPDATE SET target = excluded.target, label = excluded.label",
                     (did, target, label))
        conn.commit()
        return {"did": did, **apply_config(conn, f"Inbound route {did} -> {target}")}


@router.delete("/inbound/{did}")
def api_delete_inbound_route(did: str):
    with _lock, get_db() as conn:
        if conn.execute("DELETE FROM inbound_routes WHERE did = ?", (did,)).rowcount == 0:
            raise HTTPException(404, "Inbound route not found.")
        conn.commit()
        return apply_config(conn, f"Deleted inbound route {did}")


class SettingsRequest(BaseModel):
    outbound_cid: str
    max_outbound: int
    voxbeam_prefix: str
    trunk_order: List[str]
    trunks_enabled: Dict[str, bool]
    inbound_default: str
    routes: Optional[Dict[str, str]] = None  # number type -> ROUTE_CHOICES


@router.put("/settings")
def api_update_settings(req: SettingsRequest):
    cid = req.outbound_cid.strip().replace(" ", "")
    if cid and not CID_RE.match(cid):
        raise HTTPException(400, "Caller ID must be a phone number, e.g. +35312345678.")
    if not 1 <= req.max_outbound <= 30:
        raise HTTPException(400, "Max simultaneous outbound calls must be 1-30.")
    if req.voxbeam_prefix not in VOXBEAM_ROUTES:
        raise HTTPException(400, "Unknown Voxbeam route.")
    if sorted(req.trunk_order) != sorted(TRUNKS):
        raise HTTPException(400, "Trunk order must list each trunk once.")
    routes = req.routes or {}
    if set(routes) - set(NUMBER_TYPES) or set(routes.values()) - set(ROUTE_CHOICES):
        raise HTTPException(400, "Unknown number type or route.")
    with _lock, get_db() as conn:
        default = _valid_target(conn, req.inbound_default)
        _set_setting(conn, "outbound_cid", cid)
        _set_setting(conn, "max_outbound", req.max_outbound)
        _set_setting(conn, "voxbeam_prefix", req.voxbeam_prefix)
        _set_setting(conn, "trunk_order", ",".join(req.trunk_order))
        for t in TRUNKS:
            _set_setting(conn, f"trunk_{t}_enabled", "1" if req.trunks_enabled.get(t) else "0")
        _set_setting(conn, "inbound_default", default)
        for kind, route in routes.items():
            _set_setting(conn, f"route_{kind}", route)
        conn.commit()
        enabled = [t for t in req.trunk_order if req.trunks_enabled.get(t)]
        return apply_config(conn, f"Settings: cid={cid}, max={req.max_outbound}, voxbeam={req.voxbeam_prefix}, "
                                  f"outbound order={enabled or 'NONE'}, inbound default={default}, "
                                  f"routes={routes or 'unchanged'}")


@router.post("/apply")
def api_apply():
    """Regenerate and reload without changing anything (e.g. after editing pjsip.conf by hand)."""
    with _lock, get_db() as conn:
        return apply_config(conn, "Manual re-apply")


@public_router.get("/{token}/linphone.xml")
def prov_linphone(token: str, request: Request):
    """The account settings as a Linphone remote-provisioning file. A
    browser (someone scanning with the Camera app) gets told to scan from
    inside Linphone instead, without using the token up or seeing the
    password; Linphone's own downloader doesn't call itself Mozilla."""
    if "mozilla" in request.headers.get("user-agent", "").lower():
        with get_db() as conn:
            r = _live_token(conn, token)
        if not r:
            return HTMLResponse(f"<p>{_GONE}</p>", status_code=404, headers=_NO_STORE)
        return _linphone_page(token, r)
    e = _redeem(token, request, "Linphone")
    if not e:
        return _gone()
    server = f"&lt;sip:{x(PUBLIC_HOST)}:{x(PUBLIC_PORT)};transport=tls&gt;"
    name = x(e["name"]).replace('"', "")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<config xmlns="http://www.linphone.org/xsds/lpconfig.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.linphone.org/xsds/lpconfig.xsd lpconfig.xsd">
  <section name="misc">
    <entry name="transient_provisioning" overwrite="true">1</entry>
  </section>
  <section name="sip">
    <entry name="default_proxy" overwrite="true">0</entry>
    <entry name="media_encryption" overwrite="true">srtp</entry>
    <entry name="media_encryption_mandatory" overwrite="true">1</entry>
  </section>
  <section name="proxy_0" overwrite="true">
    <entry name="reg_proxy" overwrite="true">{server}</entry>
    <entry name="reg_route" overwrite="true">{server}</entry>
    <entry name="reg_identity" overwrite="true">"{name}" &lt;sip:{x(e["ext"])}@{x(PUBLIC_HOST)}&gt;</entry>
    <entry name="reg_expires" overwrite="true">600</entry>
    <entry name="reg_sendregister" overwrite="true">1</entry>
    <entry name="publish" overwrite="true">0</entry>
  </section>
  <section name="auth_info_0" overwrite="true">
    <entry name="username" overwrite="true">{x(e["ext"])}</entry>
    <entry name="userid" overwrite="true">{x(e["ext"])}</entry>
    <entry name="passwd" overwrite="true">{x(e["password"])}</entry>
    <entry name="domain" overwrite="true">{x(PUBLIC_HOST)}</entry>
  </section>
</config>
"""
    return Response(body, media_type="application/xml", headers=_NO_STORE)


# ---------------------------------------------------------------------------
# Setup by email (Microsoft Graph)
# ---------------------------------------------------------------------------

class SetupEmailRequest(BaseModel):
    to: str
    apps: List[str] = ["groundwire"]


def _graph_send(to: str, subject: str, body_html: str, inline_images: Dict[str, bytes]):
    """Sends one HTML email as EMAIL_FROM_ADDRESS; images are referenced
    from the body as cid:<name>. Raises HTTPException on failure."""
    try:
        with httpx.Client(timeout=20) as client:
            tok = client.post(f"https://login.microsoftonline.com/{MS_GRAPH_TENANT_ID}/oauth2/v2.0/token", data={
                "client_id": MS_GRAPH_CLIENT_ID, "client_secret": MS_GRAPH_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
            tok.raise_for_status()
            res = client.post(
                f"https://graph.microsoft.com/v1.0/users/{EMAIL_FROM_ADDRESS}/sendMail",
                headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
                json={"message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": body_html},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                    "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": f"{cid}.png",
                                     "contentType": "image/png", "contentBytes": base64.b64encode(png).decode(),
                                     "isInline": True, "contentId": cid}
                                    for cid, png in inline_images.items()],
                }, "saveToSentItems": True})
            res.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Graph's error text, never the request (it holds the secret).
        raise HTTPException(502, f"Email not sent: Microsoft Graph said {e.response.status_code} "
                                 f"{e.response.text[:200]}")
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise HTTPException(502, f"Email not sent: {type(e).__name__}")


def _qr_png(url: str) -> bytes:
    import io
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


def _setup_email(e, token: str, apps: List[str], expires: datetime):
    """The email's HTML and its inline QR images."""
    esc = html.escape
    btn = ("display:inline-block;padding:12px 20px;border-radius:8px;background:#0a7cff;color:#ffffff;"
           "text-decoration:none;font-weight:600")
    parts, images = [], {}
    if "groundwire" in apps:
        images["groundwire-qr"] = _qr_png(_setup_url(token, "groundwire"))
        parts.append(f"""
<h2 style="font-size:18px">Groundwire</h2>
<ol>
<li>Install <b>Groundwire</b> from the App Store.</li>
<li>Reading this on your iPhone? Tap <a href="{esc(_setup_url(token, 'groundwire'))}" style="{btn}">Set up in Groundwire</a>
 and then <b>Open in Groundwire</b>.</li>
<li>Reading it on a computer? Scan this with the iPhone's Camera app and tap the link:<br>
<img src="cid:groundwire-qr" alt="Groundwire setup QR code" width="200" height="200"></li>
</ol>""")
    if "linphone" in apps:
        images["linphone-qr"] = _qr_png(_setup_url(token, "linphone"))
        parts.append(f"""
<h2 style="font-size:18px">Linphone</h2>
<ol>
<li>Install <b>Linphone</b> from the App Store and open it.</li>
<li>Reading this on a computer? In Linphone tap <b>Scan QR code</b> and scan this
(not with the Camera app):<br>
<img src="cid:linphone-qr" alt="Linphone setup QR code" width="200" height="200"></li>
<li>Reading it on your iPhone? Tap <a href="{esc(PROVISION_BASE_URL)}/prov/{token}?app=linphone">this link</a>
and then <b>Open in Linphone</b>. If that doesn't work, open this email on a computer and scan the code.</li>
</ol>
<p style="color:#666">Linphone only rings while the app is open.</p>""")
    either = " Use it in one app only." if len(apps) > 1 else ""
    body = f"""<div style="font:15px -apple-system,Segoe UI,sans-serif;color:#222;max-width:560px">
<p>Hello,</p>
<p>Here's how to set up your phone app for extension <b>{esc(e["ext"])}</b> ({esc(e["name"])}).
The app fills in all the settings itself.</p>
{''.join(parts)}
<h2 style="font-size:18px">Voicemail</h2>
<p>Dial <b>{VOICEMAIL_NUMBER}</b> from the app to hear your messages. Your voicemail PIN is <b>{esc(e["vm_pin"])}</b>.</p>
<p style="color:#666">The setup link works once and expires {_local_time(expires)}.{either}
If it has expired, ask for a new one.</p>
</div>"""
    return body, images


@router.post("/extensions/{ext}/email-setup")
def api_email_setup(ext: str, req: SetupEmailRequest):
    to = req.to.strip()
    if not EMAIL_RE.match(to):
        raise HTTPException(400, "That doesn't look like an email address.")
    apps = [a for a in SETUP_APPS if a in req.apps]
    if not apps or set(req.apps) - set(SETUP_APPS):
        raise HTTPException(400, "Choose Groundwire, Linphone or both.")
    if not (MS_GRAPH_TENANT_ID and MS_GRAPH_CLIENT_ID and MS_GRAPH_CLIENT_SECRET and EMAIL_FROM_ADDRESS):
        raise HTTPException(400, "Set MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET and "
                                 "EMAIL_FROM_ADDRESS in the dashboard's .env to send setup emails.")
    _setup_ready()
    token, e, expires = _make_token(ext, EMAIL_LINK_TTL)
    body, images = _setup_email(e, token, apps, expires)
    try:
        _graph_send(to, f"Set up your phone: extension {e['ext']}", body, images)
    except HTTPException:
        with _lock, get_db() as conn:
            conn.execute("DELETE FROM provision_tokens WHERE token_hash = ?", (_token_hash(token),))
            conn.commit()
        raise
    changes_log.info(f"Emailed {'+'.join(apps)} setup for extension {ext} to {to}")
    return {"ok": True, "expires_at": expires.isoformat(),
            "messages": [f"Setup email sent to {to}. The link works until {_local_time(expires)}."]}
