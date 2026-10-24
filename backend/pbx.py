"""
Phone System (Asterisk PBX) management — the "Phone System" tab.

The dashboard keeps its own small SQLite store (pbx.db) as the source of
truth for extensions, ring groups, inbound routes and trunk settings, and
GENERATES two Asterisk include files from it on every change:

    /etc/asterisk/pjsip_extensions.conf   extensions (endpoint/auth/aor)
    /etc/asterisk/extensions_portal.conf  [globals], ring groups, inbound
                                          routing, outbound trunk order

then reloads Asterisk (pjsip reload + dialplan reload — neither drops
calls in progress). Everything else in /etc/asterisk (transports, the
trunk definitions themselves, the static dialplan in extensions.conf) is
still edited by hand and never touched from here.

First run: if pbx.db is empty, the extensions already in
pjsip_extensions.conf (from make_extensions.py) and the [globals] values in
extensions.conf are imported, so existing phones keep their passwords.

Live status comes from `asterisk -rx` CLI commands. This works because the
dashboard service runs as root, same as its other reads on this VPS.
"""

import os
import re
import pwd
import grp
import secrets
import sqlite3
import logging
import logging.handlers
import subprocess
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
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

EXT_CONF = os.path.join(ASTERISK_DIR, "pjsip_extensions.conf")
PORTAL_DIALPLAN = os.path.join(ASTERISK_DIR, "extensions_portal.conf")
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

DEFAULT_SETTINGS = {
    "outbound_cid": "",
    "max_outbound": "4",
    "voxbeam_prefix": "0011101",
    "trunk_order": "irishvoip,voxbeam",
    "trunk_irishvoip_enabled": "1",
    "trunk_voxbeam_enabled": "1",
    "inbound_default": "201",
}

NAME_RE = re.compile(r"^[A-Za-z0-9 .'_-]{1,40}$")
CID_RE = re.compile(r"^\+?\d{6,15}$")

_lock = threading.Lock()
_last_apply: dict = {"at": None, "ok": None, "messages": []}

router = APIRouter(prefix="/api/pbx")


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
               value TEXT NOT NULL);"""
    )
    if fresh:
        os.chmod(PBX_DB_PATH, 0o600)  # holds SIP passwords
    if conn.execute("SELECT 1 FROM settings WHERE key = 'imported'").fetchone() is None:
        _import_existing(conn)
    return conn


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
                "",
                f"[{r['ext']}](auth-ext)",
                f"username={r['ext']}",
                f"password={r['password']}",
                "",
                f"[{r['ext']}](aor-ext)",
                ""]
    return "\n".join(out)


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
        out.append(" same => n,Hangup()")

    out += ["", "; ---- Outbound: trunks tried in order ----",
            "; ARG1 = national number, ARG2 = international (353...)",
            "[sub-outbound]",
            "exten => s,1,Set(GROUP(outbound)=calls)",
            " same => n,GotoIf($[${GROUP_COUNT(calls@outbound)} > ${MAX_OUTBOUND}]?busy)"]
    if s["outbound_cid"]:
        out.append(" same => n,Set(CALLERID(num)=${OUTBOUND_CID})")
    enabled = [t for t in s["trunk_order"].split(",") if t in TRUNKS and s.get(f"trunk_{t}_enabled") == "1"]
    if not enabled:
        out.append(" same => n,NoOp(No outbound trunks enabled)")
        out.append(" same => n,Goto(busy)")
    for i, t in enumerate(enabled):
        last = i == len(enabled) - 1
        out.append(f" same => n,Dial(PJSIP/{TRUNKS[t]['dial']}@{t},60)")
        if not last:
            # Only fall through to the next trunk if this one couldn't take
            # the call at all (down, refused, congested), never after a
            # normal answer/busy/no-answer.
            out.append(' same => n,GotoIf($["${DIALSTATUS}" != "CHANUNAVAIL" & "${DIALSTATUS}" != "CONGESTION"]?done)')
    out += [" same => n(done),Hangup()",
            " same => n(busy),Congestion(5)",
            " same => n,Hangup()",
            "",
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
    try:
        uid, gid = pwd.getpwnam("asterisk").pw_uid, grp.getgrnam("asterisk").gr_gid
        os.chown(tmp, uid, gid)
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
                             "ring_time": r["ring_time"], "members": _members(r)}
                            for r in conn.execute("SELECT * FROM ring_groups ORDER BY number")],
            "inbound_routes": [dict(r) for r in conn.execute("SELECT * FROM inbound_routes ORDER BY did")],
            "settings": {
                "outbound_cid": s["outbound_cid"],
                "max_outbound": int(s["max_outbound"]),
                "voxbeam_prefix": s["voxbeam_prefix"],
                "trunk_order": [t for t in s["trunk_order"].split(",") if t in TRUNKS],
                "trunks_enabled": {t: s.get(f"trunk_{t}_enabled") == "1" for t in TRUNKS},
                "inbound_default": s["inbound_default"],
            },
            "trunks": {k: {"label": v["label"], "host": v["host"]} for k, v in TRUNKS.items()},
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
        conn.execute("INSERT INTO extensions (ext, name, password, enabled, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                     (ext, name, secrets.token_urlsafe(24), now, now))
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
        conn.commit()
        return apply_config(conn, f"Updated extension {ext}: name={name}, enabled={req.enabled}")


@router.post("/extensions/{ext}/reset-password")
def api_reset_password(ext: str):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET password = ?, updated_at = ? WHERE ext = ?",
                        (secrets.token_urlsafe(24), _now(), ext)).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"Reset password for extension {ext}")


@router.get("/extensions/{ext}/credentials")
def api_credentials(ext: str):
    with get_db() as conn:
        r = conn.execute("SELECT ext, password FROM extensions WHERE ext = ?", (ext,)).fetchone()
    if not r:
        raise HTTPException(404, "Extension not found.")
    changes_log.info(f"Viewed credentials for extension {ext}")
    return {"username": r["ext"], "password": r["password"], "server": PUBLIC_HOST,
            "port": PUBLIC_PORT, "transport": "TLS", "media_encryption": "SRTP (SDES)"}


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
        conn.commit()
        return apply_config(conn, f"Deleted extension {ext}")


class RingGroupRequest(BaseModel):
    number: Optional[str] = None  # next free number if omitted (create only)
    name: str
    strategy: str = "ringall"
    ring_time: int = 25
    members: List[str] = []


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
    return {"name": _clean_name(req.name), "strategy": req.strategy,
            "ring_time": req.ring_time, "members": ",".join(members)}


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
        conn.execute("INSERT INTO ring_groups (number, name, strategy, ring_time, members) VALUES (?, ?, ?, ?, ?)",
                     (number, g["name"], g["strategy"], g["ring_time"], g["members"]))
        conn.commit()
        result = apply_config(conn, f"Created ring group {number} ({g['name']}): {g['strategy']} {g['members']}")
    return {"number": number, **result}


@router.put("/ringgroups/{number}")
def api_update_group(number: str, req: RingGroupRequest):
    with _lock, get_db() as conn:
        g = _clean_group(conn, req)
        if conn.execute("UPDATE ring_groups SET name = ?, strategy = ?, ring_time = ?, members = ? WHERE number = ?",
                        (g["name"], g["strategy"], g["ring_time"], g["members"], number)).rowcount == 0:
            raise HTTPException(404, "Ring group not found.")
        conn.commit()
        return apply_config(conn, f"Updated ring group {number}: {g['strategy']} {g['members']}")


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
    with _lock, get_db() as conn:
        default = _valid_target(conn, req.inbound_default)
        _set_setting(conn, "outbound_cid", cid)
        _set_setting(conn, "max_outbound", req.max_outbound)
        _set_setting(conn, "voxbeam_prefix", req.voxbeam_prefix)
        _set_setting(conn, "trunk_order", ",".join(req.trunk_order))
        for t in TRUNKS:
            _set_setting(conn, f"trunk_{t}_enabled", "1" if req.trunks_enabled.get(t) else "0")
        _set_setting(conn, "inbound_default", default)
        conn.commit()
        enabled = [t for t in req.trunk_order if req.trunks_enabled.get(t)]
        return apply_config(conn, f"Settings: cid={cid}, max={req.max_outbound}, voxbeam={req.voxbeam_prefix}, "
                                  f"outbound order={enabled or 'NONE'}, inbound default={default}")


@router.post("/apply")
def api_apply():
    """Regenerate and reload without changing anything (e.g. after editing pjsip.conf by hand)."""
    with _lock, get_db() as conn:
        return apply_config(conn, "Manual re-apply")
