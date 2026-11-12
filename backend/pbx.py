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
import json
import base64
import contextvars
import email
import email.policy
import email.utils
import hashlib
import time
import pwd
import grp
import secrets
import shutil
import sqlite3
import logging
import logging.handlers
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple
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
# Tenants' own SIP hostnames: when set, each must be one name under this
# (".sip.example.ie" -> acme.sip.example.ie), which is what a wildcard
# certificate for *.sip.example.ie covers. The main tenant's phones use
# PBX_PUBLIC_HOST unless it's given one too.
TENANT_HOST_SUFFIX = os.environ.get("PBX_TENANT_HOST_SUFFIX", "").strip().lower()
# Public address of the QR-code setup links (see "Phone setup by QR code"
# in the README), e.g. https://amapforyourmoney.com. Empty turns them off.
PROVISION_BASE_URL = os.environ.get("PBX_PROVISION_BASE_URL", "").rstrip("/")
PROVISION_TTL = timedelta(minutes=15)
# Emailed links last longer: people don't read email within 15 minutes.
EMAIL_LINK_TTL = timedelta(hours=int(os.environ.get("PBX_EMAIL_LINK_HOURS", "72")))

# Setup emails go out through Microsoft Graph (an app registration allowed
# to send as EMAIL_FROM_ADDRESS).
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
# Parking lots of tenants other than the main one (which uses Asterisk's
# own [default] lot), included from res_parking.conf.
PARKING_CONF = os.path.join(GENERATED_DIR, "res_parking_portal.conf")
VM_CONTEXT = "portal"
# Where phones' SUBSCRIBEs for busy lamps (BLF) and presence are looked
# up: only the hints, so a phone can watch an extension but can't reach
# anything else in the dialplan this way.
HINTS_CONTEXT = "portal-hints"
PICKUP_GROUP = "portal"  # every extension; *8 answers any of them ringing
# Do not disturb and call forwarding live in Asterisk's own database
# (AstDB), so the feature codes on the phones and the dashboard change the
# same thing. Families as FreePBX uses them.
# Call links: anyone with the link calls from their browser (WebRTC) as the
# one guest endpoint. Its password is in every call page, so it is public:
# what keeps it safe is that it can only reach [portal-weblinks], which has
# nothing but the enabled links' targets (never a trunk).
WEB_GUEST = "webguest"
WEBLINKS_CONTEXT = "portal-weblinks"
WEBLINK_MAX_PER_LINK = 2  # calls at once through one link
# Browser calls aren't PSTN calls (no carrier channel), so they don't count
# towards a tenant's calls at once, its plan. They're limited only to
# protect the server and other tenants (call links are public): per
# tenant (web_max, its own setting) and over all of them (web_max_total).
WEB_MAX_LIMIT, WEB_MAX_TOTAL_LIMIT = 50, 200

FORWARD_FAMILIES = {"always": "CF", "busy": "CFB", "no_answer": "CFU"}
DND_FAMILY = "DND"
MAX_FORWARD_HOPS = 3  # 201 -> 202 -> 201 ... stops here, at voicemail
# Where calls may be forwarded: other extensions and Irish landlines and
# mobiles (national format). Never abroad, 1800/premium or 112/999: anyone
# who rings your number can set a forward going, at your cost.
FORWARD_TARGET_DIALPLAN_RE = "^(2[0-9][0-9]|0[1-9][0-9]{7}[0-9]?)$"
VOICEMAIL_NUMBER = "*97"  # check your own voicemail (static dialplan)
# Asterisk hands each "new voicemail" email to asterisk/vm-mail.sh (mailcmd
# in voicemail.conf), which drops it here; the dashboard sends it on
# through Microsoft Graph (see "Voicemail by email" below).
VM_OUTBOX = os.environ.get("PBX_VM_OUTBOX", "/var/spool/asterisk/vm-outbox")
# PINs changed from a phone's voicemail menu: asterisk/vm-pin.sh (Asterisk's
# externpass) drops "<context> <mailbox> <pin>" here, and vm_pin_loop saves
# each like a PIN chosen in the portal.
VM_PINS = os.environ.get("PBX_VM_PINS", "/var/spool/asterisk/vm-pins")
STATIC_DIALPLAN = os.path.join(ASTERISK_DIR, "extensions.conf")

# Must line up with the static dialplan: _2XX dials extensions, and ring
# groups live in their own 6XX range so the two can never collide.
EXT_MIN, EXT_MAX = 200, 299
GROUP_MIN, GROUP_MAX = 600, 699
# IVR menus: 5XX (7XX is Asterisk's default parking lot).
MENU_MIN, MENU_MAX = 500, 599
MENU_KEYS = "1234567890*#"
MAX_MENU_HOPS = 20  # menus entered in one call, so a fallback loop ends
# Greetings, one per menu (ivr-<number>.ulaw: 8 kHz mu-law, no header).
# The dashboard page converts uploads and recordings to that, so nothing
# here ever reads an uploaded audio file; any bytes are valid mu-law.
IVR_DIR = os.path.join(GENERATED_DIR, "ivr")
# Call park: Asterisk's own lot (res_parking.conf, left at its defaults):
# transfer a call to 700, pick it up again on the slot it reads out.
PARK_LOT, PARK_CONTEXT, PARK_EXT = "default", "parkedcalls", "700"
PARK_SLOTS = [str(n) for n in range(701, 721)]
# Call recording: Asterisk (MixMonitor) writes 8 kHz mu-law files here,
# named <time>_<ext>_<other party>_<in|out|int|req>_<uniqueid>.ulaw; the
# dashboard lists, plays (as WAV), deletes and expires them. A folder of
# its own, mode 2770 asterisk:asterisk (README, "Call recording").
REC_DIR = os.environ.get("PBX_RECORDINGS_DIR", "/var/spool/asterisk/monitor/portal")
REC_NAME_RE = re.compile(r"(\d{8}-\d{6})_(\d{3})_(\d{0,20}|unknown)_(in|out|int|req)_([0-9.]{1,40})\.ulaw")
# Told to outside callers when calls may be recorded (an IVR-style greeting).
REC_NOTICE = os.path.join(IVR_DIR, "recording-notice.ulaw")
# Business hours schedules: 4XX. Open or closed by the clock in PBX_TIMEZONE
# (the VPS's own clock may be in another zone).
HOURS_MIN, HOURS_MAX = 400, 499
PBX_TIMEZONE = os.environ.get("PBX_TIMEZONE", "Europe/Dublin")
if not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", PBX_TIMEZONE):  # it goes into the dialplan
    PBX_TIMEZONE = "Europe/Dublin"

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
#   trunk; cheapest = every route ticked for it, cheapest first (lcr.py);
#   off = refused. Every call is checked against the price cap first (see
#   _outbound_block and lcr.py).
NUMBER_TYPES = {
    "mobile": "Irish mobiles (083, 085, 086, 087, 089)",
    "landline": "Irish landlines and other Irish numbers (01, 021, 0818...)",
    "freephone": "Irish freephone (1800)",
    "international": "International (00 or +, outside Ireland)",
}
ROUTE_CHOICES = ["default", *TRUNKS, "cheapest", "off"]
# Every route a call can take: Irish VoIP, and each Voxbeam route.
ALL_ROUTES = [("irishvoip", ""), *[("voxbeam", p) for p in VOXBEAM_ROUTES]]
# Where Asterisk asks for the routes (lcr.py), i.e. this service's own address.
LCR_URL = os.environ.get("PBX_LCR_URL", "http://127.0.0.1:8010/lcr/route")

# Tenants (multi-tenant groundwork, phase 1): every tenant-owned row says
# whose it is. Everything today belongs to the main tenant (code 1001);
# nothing reads tenant_id yet.
MAIN_TENANT, MAIN_TENANT_CODE = 1, "1001"
TENANT_TABLES = ("extensions", "ring_groups", "inbound_routes", "provision_tokens", "call_links", "ivr_menus",
                 "business_hours")

# Settings each tenant has its own of (tenant_settings); everything else in
# settings (trunks, routes, video, the default inbound route) is the server's.
TENANT_SETTINGS = ("outbound_cid", "max_outbound", "rec_days", "cap_default", *[f"cap_{t}" for t in NUMBER_TYPES],
                   "main_mailbox", "web_max")


C2C_RING = "portal-c2c-ring"
C2C_DIAL = "portal-c2c-dial"


class TenantNames:
    """Every Asterisk name (contexts, voicemail context, pickup group, park
    lot) and folder that belongs to one tenant, so tenants can't reach each
    other's. The main tenant keeps the names it had before there were
    tenants (portal-..., voicemail "portal", park lot "default"); any other
    gets t<code>-... and folders of its own."""

    def __init__(self, tenant_id: int, code: str):
        self.id, self.code = tenant_id, code
        main = tenant_id == MAIN_TENANT
        self.prefix = "portal" if main else f"t{code}"
        self.vm = VM_CONTEXT if main else self.prefix
        self.pickup = PICKUP_GROUP if main else self.prefix
        self.hints = HINTS_CONTEXT if main else f"{self.prefix}-hints"
        self.call_ext = f"{self.prefix}-call-ext"
        self.features = f"{self.prefix}-features"
        self.ringgroups = f"{self.prefix}-ringgroups"
        self.park_lot = PARK_LOT if main else f"{self.prefix}-park"
        self.park_context = PARK_CONTEXT if main else f"{self.prefix}-parkedcalls"
        # Every tenant's greetings and recordings in a folder of its own,
        # t<code>, the main tenant's too (since phase 3; they were in the
        # top folders, see _tenant_folders).
        self.ivr_dir = os.path.join(IVR_DIR, f"t{code}")
        self.rec_dir = os.path.join(REC_DIR, f"t{code}")
        self.notice = os.path.join(self.ivr_dir, "recording-notice.ulaw")
        # Phones log in as tenant code + extension (1002201): a login is
        # unique on the server. The main tenant's are still the bare
        # extension (201) until they're moved over (phase 3).
        self.login_prefix = "" if main else code
        # Where the tenant's phones (and its forwards and click to call)
        # dial from: its generated [<prefix>-internal]. The main tenant's
        # phones still start in the hand-written [from-internal] until that
        # just includes [portal-internal] (multi-tenant step 2.5).
        self.internal = f"{self.prefix}-internal"
        self.dial_from = "from-internal" if main else self.internal
        self.c2c_ring = C2C_RING if main else f"{self.prefix}-c2c-ring"
        self.c2c_dial = C2C_DIAL if main else f"{self.prefix}-c2c-dial"

    def login(self, ext: str) -> str:
        return f"{self.login_prefix}{ext}"

    def accountcode(self) -> List[str]:
        """Dialplan lines marking a call as this tenant's for billing (CDR
        accountcode). None for the main tenant: its calls have always had
        an empty account code, which billing reads as the main tenant's."""
        return [] if self.id == MAIN_TENANT else [f" same => n,Set(CHANNEL(accountcode)={self.code})"]

    def ivr(self, number: str) -> str:
        return f"{self.prefix}-ivr-{number}"

    def hours(self, number: str) -> str:
        return f"{self.prefix}-hours-{number}"


MAIN_NAMES = TenantNames(MAIN_TENANT, MAIN_TENANT_CODE)

# The tenant the dashboard is working on, per request: always the main one
# until the tenant switcher (phase 2) sets it. Every save and lookup in the
# dashboard goes through it; the config generators take tenants explicitly.
_current_tenant = contextvars.ContextVar("pbx_tenant", default=MAIN_TENANT)
# Who made a change through the tenant portal (main.py, X-Portal-User):
# only ever written into the change log, never trusted for anything.
_acting_user = contextvars.ContextVar("pbx_acting_user", default="")


def current_tenant() -> int:
    return _current_tenant.get()


def tenant_names(conn, tenant: int) -> TenantNames:
    if tenant == MAIN_TENANT:
        return MAIN_NAMES
    r = conn.execute("SELECT code FROM tenants WHERE id = ?", (tenant,)).fetchone()
    if not r:
        raise HTTPException(404, "Tenant not found.")
    return TenantNames(tenant, r["code"])


def all_tenant_names(conn) -> List[TenantNames]:
    """Every tenant, the main one first."""
    return [TenantNames(r["id"], r["code"]) for r in conn.execute("SELECT id, code FROM tenants ORDER BY id")]


# Phones' logins (endpoint names) and the extensions they are. Today a
# login is the extension itself (201); when logins become tenant code +
# extension (1001201), only these change. The dialplan never works it out:
# each endpoint carries PBX_EXT and PBX_TENANT (set_var), which Asterisk
# sets on its channels both ways, calling and called.
def parse_login(login: str) -> Optional[Tuple[str, str]]:
    """(tenant code, extension) of a phone login: 201 is the main tenant's,
    1002201 tenant 1002's. None for anything else (a trunk, webguest)."""
    if re.fullmatch(r"2[0-9]{2}", login or ""):
        return MAIN_TENANT_CODE, login
    m = re.fullmatch(r"([0-9]{4})(2[0-9]{2})", login or "")
    return (m.group(1), m.group(2)) if m else None


def endpoint_ext(endpoint: str, code: str = MAIN_TENANT_CODE) -> Optional[str]:
    """The extension a login is, if it's one of tenant code's."""
    p = parse_login(endpoint)
    return p[1] if p and p[0] == code else None


def channel_ext(channel: str, code: str = MAIN_TENANT_CODE) -> Optional[str]:
    """The extension a channel (PJSIP/201-0000001a) is a call of, if it's
    tenant code's, or None: another tenant's, a trunk, a browser, a Local
    channel."""
    m = re.fullmatch(r"PJSIP/([^-/]+)-[0-9a-f]+", channel or "")
    return endpoint_ext(m.group(1), code) if m else None


def sip_host_for(tenant: int) -> str:
    """Where a tenant's phones register: its own hostname (so it can be
    moved to another server by DNS), else the server's. Phones already set
    up keep what they were given until they're set up again."""
    with get_db() as conn:
        r = conn.execute("SELECT sip_host FROM tenants WHERE id = ?", (tenant,)).fetchone()
    return (r and r["sip_host"]) or PUBLIC_HOST


def row_login(row) -> str:
    """The login of an extensions row."""
    if row["tenant_id"] == MAIN_TENANT:
        return row["ext"]
    with get_db() as conn:
        return tenant_names(conn, row["tenant_id"]).login(row["ext"])

VIDEO_CODECS = "h264,vp8"  # both apps offer these; H.264 first: iPhones encode it in hardware

DEFAULT_SETTINGS = {
    "outbound_cid": "",
    "max_outbound": "4",
    "voxbeam_prefix": "0011101",
    "trunk_order": "irishvoip,voxbeam",
    "trunk_irishvoip_enabled": "1",
    "trunk_voxbeam_enabled": "1",
    # Most calls at once each carrier allows (its channels, in and out
    # together); empty = no ceiling here. See _trunk_ceilings.
    "trunk_max_irishvoip": "",
    "trunk_max_voxbeam": "",
    "inbound_default": "201",
    # Takes incoming calls over a tenant's limit (not the main tenant's: its
    # limit counts outbound calls only). Empty = its lowest extension.
    "main_mailbox": "",
    "web_max": "10",  # browser calls at once, per tenant (call links)
    "web_max_total": "30",  # browser calls at once, all tenants together
    "rec_days": "90",  # recordings older than this are deleted
    "route_mobile": "irishvoip",
    "route_landline": "default",
    "route_freephone": "irishvoip",
    "route_international": "voxbeam",
    # Price cap, € ex VAT per minute: the default, and per number type
    # (cap_mobile etc., empty = the default).
    "cap_default": "0.05",
    # Video between extensions: Asterisk passes it through untouched (no
    # transcoding), so both phones must share a codec. Off until tested.
    "video": "0",
    **{f"lcr_allow_{t}{r}": "1" for t, r in ALL_ROUTES},  # usable for "cheapest"
}


def lcr_routes(s: Dict[str, str], mode: str):
    """The (trunk, route) candidates for a number type's route setting, in
    that setting's order; Voxbeam's route is the chosen one except for
    "cheapest", which may use any ticked route."""
    enabled = [t for t in s["trunk_order"].split(",") if t in TRUNKS and s.get(f"trunk_{t}_enabled") == "1"]
    if mode == "cheapest":
        return [(t, r) for t, r in ALL_ROUTES if t in enabled and s.get(f"lcr_allow_{t}{r}", "1") == "1"]
    if mode not in ("default", *TRUNKS):
        return []
    return [(t, s["voxbeam_prefix"] if t == "voxbeam" else "") for t in enabled if mode == "default" or t == mode]


def classify_dialled(dialled: str):
    """(number type, national form, international without 00) for a number
    as dialled, sorted the way the static dialplan does it; (None, ...) if
    it would be refused there."""
    d = re.sub(r"[\s()-]", "", dialled or "")
    if d in ("112", "999"):
        return "emergency", d, d
    if d.startswith("+353"):
        d = "0" + d[4:].lstrip("0")
    elif d.startswith("00353"):
        d = "0" + d[5:].lstrip("0")
    if re.fullmatch(r"08[35679]\d{7}", d):
        return "mobile", d, "353" + d[1:]
    if re.fullmatch(r"0[1-9]\d{7,8}", d):
        return "landline", d, "353" + d[1:]
    if re.fullmatch(r"01800\d{6}", d):  # +353 1800 ... came out as 01800...
        d = d[1:]
    if re.fullmatch(r"1800\d{6}", d):
        return "freephone", d, "353" + d
    m = re.fullmatch(r"(?:00|\+)([1-9]\d{6,})", d)
    if m:
        return "international", "00" + m.group(1), m.group(1)
    return None, d, d

NAME_RE = re.compile(r"^[A-Za-z0-9 .'_-]{1,40}$")
CID_RE = re.compile(r"^\+?\d{6,15}$")

_lock = threading.Lock()
_last_apply: dict = {"at": None, "ok": None, "messages": []}

router = APIRouter(prefix="/api/pbx")
# The only routes here reachable without the dashboard's login: a phone
# scanning a setup QR code isn't on Tailscale. Outside /api/, so
# require_auth leaves them alone; each needs a valid one-time token.
public_router = APIRouter(prefix="/prov")


class _ActingUser(logging.Filter):
    """Changes made through the tenant portal say who, for the change log."""
    def filter(self, record):
        who = _acting_user.get()
        if who:
            record.msg = f"{record.getMessage()} [portal: {who}]"
            record.args = None
        return True


def _changes_logger() -> logging.Logger:
    logger = logging.getLogger("pbx_changes")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(f, _ActingUser) for f in logger.filters):
        logger.addFilter(_ActingUser())
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

class MissingDatabase(RuntimeError):
    pass


def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(PBX_DB_PATH) or ".", exist_ok=True)
    fresh = not os.path.exists(PBX_DB_PATH)
    # A new, empty database where a config was already generated means
    # PBX_DB points at the wrong place (a typo, a move to another service or
    # server): applying from it would rewrite the live config with no
    # extensions. Refuse, unless told on purpose (PBX_ALLOW_NEW_DB=1).
    if fresh and os.path.exists(EXT_CONF) and os.environ.get("PBX_ALLOW_NEW_DB") != "1":
        raise MissingDatabase(f"The phone system's database isn't at {PBX_DB_PATH}, but {GENERATED_DIR} already has a "
                              "generated config: refusing to start from an empty one, which would remove every "
                              "extension. Check PBX_DB (or set PBX_ALLOW_NEW_DB=1 if a new database is really meant).")
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
               used_at TEXT);
           CREATE TABLE IF NOT EXISTS call_links (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               token TEXT NOT NULL UNIQUE,
               label TEXT NOT NULL,
               target TEXT NOT NULL,
               video INTEGER NOT NULL DEFAULT 0,
               max_minutes INTEGER NOT NULL DEFAULT 30,
               enabled INTEGER NOT NULL DEFAULT 1,
               created_at TEXT NOT NULL,
               last_opened_at TEXT);
           CREATE TABLE IF NOT EXISTS ivr_menus (
               number TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               options TEXT NOT NULL DEFAULT '{}',
               fallback TEXT NOT NULL DEFAULT '',
               timeout INTEGER NOT NULL DEFAULT 6,
               retries INTEGER NOT NULL DEFAULT 3,
               direct_dial INTEGER NOT NULL DEFAULT 0);
           CREATE TABLE IF NOT EXISTS business_hours (
               number TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               week TEXT NOT NULL DEFAULT '{}',
               holidays TEXT NOT NULL DEFAULT '[]',
               open_target TEXT NOT NULL,
               closed_target TEXT NOT NULL,
               override TEXT NOT NULL DEFAULT '');
           CREATE TABLE IF NOT EXISTS tenants (
               id INTEGER PRIMARY KEY,
               code TEXT NOT NULL UNIQUE,
               name TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'active',
               sip_host TEXT NOT NULL DEFAULT '',
               node TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL);
           CREATE TABLE IF NOT EXISTS tenant_settings (
               tenant_id INTEGER NOT NULL,
               key TEXT NOT NULL,
               value TEXT NOT NULL,
               PRIMARY KEY (tenant_id, key));"""
    )
    if fresh:
        os.chmod(PBX_DB_PATH, 0o600)  # holds SIP passwords
    for column in ("extensions ADD COLUMN vm_pin TEXT",
                   "extensions ADD COLUMN vm_email TEXT NOT NULL DEFAULT ''",
                   "extensions ADD COLUMN setup_email TEXT NOT NULL DEFAULT ''",
                   "extensions ADD COLUMN outbound_cid TEXT NOT NULL DEFAULT ''",
                   "ring_groups ADD COLUMN voicemail TEXT NOT NULL DEFAULT ''",
                   "extensions ADD COLUMN record INTEGER NOT NULL DEFAULT 0",
                   # *97 from the extension's own phones without the PIN
                   "extensions ADD COLUMN vm_nopin INTEGER NOT NULL DEFAULT 0",
                   *[f"{t} ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT {MAIN_TENANT}" for t in TENANT_TABLES]):
        try:
            conn.execute(f"ALTER TABLE {column}")
        except sqlite3.OperationalError:
            pass  # already there
    # Look before writing: an INSERT that changes nothing would still leave
    # a write transaction (and the database's lock) open on this connection.
    if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (MAIN_TENANT,)).fetchone() is None:
        conn.execute("INSERT INTO tenants (id, code, name, created_at) VALUES (?, ?, ?, ?)",
                     (MAIN_TENANT, MAIN_TENANT_CODE, "Main", _now()))
        conn.commit()
    _move_tenant_settings(conn)
    _key_by_tenant(conn)
    if conn.execute("SELECT 1 FROM settings WHERE key = 'imported'").fetchone() is None:
        _import_existing(conn)
    missing = conn.execute("SELECT rowid FROM extensions WHERE vm_pin IS NULL").fetchall()
    for r in missing:  # by row: the same number can be several tenants' extension
        conn.execute("UPDATE extensions SET vm_pin = ? WHERE rowid = ?", (_new_pin(), r["rowid"]))
    if missing:
        conn.commit()
    return conn


def _new_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(6))


def get_settings(conn, tenant: int = MAIN_TENANT) -> Dict[str, str]:
    """The server's settings with the tenant's own (TENANT_SETTINGS) on top."""
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    s.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM tenant_settings WHERE tenant_id = ?",
                                                           (tenant,))})
    return s


def _set_setting(conn, key: str, value: str, tenant: Optional[int] = None):
    """A tenant's own setting (TENANT_SETTINGS) is the current tenant's
    unless tenant says whose; anything else is the server's."""
    if key in TENANT_SETTINGS:
        tenant = current_tenant() if tenant is None else tenant
        conn.execute("INSERT INTO tenant_settings (tenant_id, key, value) VALUES (?, ?, ?) "
                     "ON CONFLICT(tenant_id, key) DO UPDATE SET value = excluded.value", (tenant, key, str(value)))
    else:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))


def _move_tenant_settings(conn):
    """One-off (multi-tenant step 2): the per-tenant settings so far were
    the main tenant's; move them out of the server-wide table."""
    if conn.execute("SELECT 1 FROM settings WHERE key = 'tenant_settings_moved'").fetchone():
        return
    marks = ", ".join("?" * len(TENANT_SETTINGS))
    conn.execute(f"INSERT OR IGNORE INTO tenant_settings (tenant_id, key, value) "
                 f"SELECT ?, key, value FROM settings WHERE key IN ({marks})", (MAIN_TENANT, *TENANT_SETTINGS))
    conn.execute(f"DELETE FROM settings WHERE key IN ({marks})", TENANT_SETTINGS)
    _set_setting(conn, "tenant_settings_moved", "1")
    conn.commit()


# Keyed by tenant + number, so two tenants can each have a 201 (multi-tenant
# phase 2). Columns in the order the older layout ended up with, so rows
# read the same. Inbound numbers, call links and setup tokens keep their
# own keys: each is unique across tenants anyway.
TENANT_KEYED = {
    "extensions": ("ext", """(ext TEXT NOT NULL, name TEXT NOT NULL, password TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, vm_pin TEXT,
        vm_email TEXT NOT NULL DEFAULT '', setup_email TEXT NOT NULL DEFAULT '', outbound_cid TEXT NOT NULL DEFAULT '',
        record INTEGER NOT NULL DEFAULT 0, vm_nopin INTEGER NOT NULL DEFAULT 0, tenant_id INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (tenant_id, ext))"""),
    "ring_groups": ("number", """(number TEXT NOT NULL, name TEXT NOT NULL, strategy TEXT NOT NULL DEFAULT 'ringall',
        ring_time INTEGER NOT NULL DEFAULT 25, members TEXT NOT NULL DEFAULT '', voicemail TEXT NOT NULL DEFAULT '',
        tenant_id INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (tenant_id, number))"""),
    "ivr_menus": ("number", """(number TEXT NOT NULL, name TEXT NOT NULL, options TEXT NOT NULL DEFAULT '{}',
        fallback TEXT NOT NULL DEFAULT '', timeout INTEGER NOT NULL DEFAULT 6, retries INTEGER NOT NULL DEFAULT 3,
        direct_dial INTEGER NOT NULL DEFAULT 0, tenant_id INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (tenant_id, number))"""),
    "business_hours": ("number", """(number TEXT NOT NULL, name TEXT NOT NULL, week TEXT NOT NULL DEFAULT '{}',
        holidays TEXT NOT NULL DEFAULT '[]', open_target TEXT NOT NULL, closed_target TEXT NOT NULL,
        override TEXT NOT NULL DEFAULT '', tenant_id INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (tenant_id, number))"""),
}


def _keyed_by_tenant(conn, table: str) -> bool:
    pk = sorted((r["pk"], r["name"]) for r in conn.execute(f"PRAGMA table_info({table})") if r["pk"])
    return [name for _, name in pk] == ["tenant_id", TENANT_KEYED[table][0]]


def _key_by_tenant(conn):
    """One-off: rebuild the tables still keyed by number alone, all in one
    transaction (copy, check the counts, swap), or none of them. A plain
    read decides first, so connections after that take no lock."""
    if all(_keyed_by_tenant(conn, t) for t in TENANT_KEYED):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = []
        for table, (_, columns) in TENANT_KEYED.items():
            if _keyed_by_tenant(conn, table):
                continue  # another process got there while this one waited
            names = ", ".join(r["name"] for r in conn.execute(f"PRAGMA table_info({table})"))
            before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"CREATE TABLE {table}_by_tenant {columns}")
            conn.execute(f"INSERT INTO {table}_by_tenant ({names}) SELECT {names} FROM {table}")
            if conn.execute(f"SELECT COUNT(*) FROM {table}_by_tenant").fetchone()[0] != before:
                raise RuntimeError(f"{table}: rows went missing while re-keying")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_by_tenant RENAME TO {table}")
            done.append(f"{table} ({before})")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if done:
        changes_log.info(f"Keyed by tenant and number: {', '.join(done)}")


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


def menu_options(row) -> Dict[str, str]:
    try:
        return {k: v for k, v in json.loads(row["options"] or "{}").items() if k in MENU_KEYS and v}
    except (ValueError, AttributeError):
        return {}


def greeting_path(number: str, t: TenantNames = MAIN_NAMES) -> str:
    return os.path.join(t.ivr_dir, f"ivr-{number}.ulaw")


# ---- Destinations: what a number in the dashboard (an inbound route, a
# call link, an IVR key, a schedule's open/closed) can be, in one place.
# Always within one tenant: its 201 is never another tenant's 201.

def destinations(conn, tenant: Optional[int] = None) -> dict:
    tenant = current_tenant() if tenant is None else tenant
    q = lambda sql: conn.execute(sql, (tenant,))
    return {"exts": {r["ext"] for r in q("SELECT ext FROM extensions WHERE tenant_id = ?")},
            "active": {r["ext"] for r in q("SELECT ext FROM extensions WHERE enabled = 1 AND tenant_id = ?")},
            "groups": {r["number"] for r in q("SELECT number FROM ring_groups WHERE tenant_id = ?")},
            "menus": {r["number"] for r in q("SELECT number FROM ivr_menus WHERE tenant_id = ?")},
            "hours": {r["number"] for r in q("SELECT number FROM business_hours WHERE tenant_id = ?")}}


def target_goto(target: str, d: dict, t: TenantNames = MAIN_NAMES) -> Optional[str]:
    """The dialplan step that sends a call to target, or None if it's gone
    (deleted, or an extension switched off). d and t: the same tenant's."""
    if target == "hangup":
        return "Hangup()"
    if target.startswith("vm:"):
        return f"VoiceMail({target[3:]}@{t.vm},u)" if target[3:] in d["active"] else None
    if target in d["active"]:
        return f"Goto({t.call_ext},{target},1)"
    if target in d["groups"]:
        return f"Goto({t.ringgroups},{target},1)"
    if target in d["menus"]:
        return f"Goto({t.ivr(target)},s,1)"
    if target in d["hours"]:
        return f"Goto({t.hours(target)},s,1)"
    return None


def check_target(conn, target: str, *, extras: bool = False, schedules: bool = True, itself: str = "",
                 tenant: Optional[int] = None) -> str:
    """target, if it's somewhere a call can be sent, else a 400. extras: an
    extension's voicemail (vm:201) and hang up too. schedules=False for a
    schedule's own targets: one schedule handing to another has no hop
    count to stop a loop. itself: a menu may send callers back to itself."""
    target = (target or "").strip()
    d = destinations(conn, tenant)
    ok = (target in d["exts"] or target in d["groups"] or target in d["menus"]
          or (schedules and target in d["hours"]) or (itself and target == itself)
          or (extras and (target == "hangup" or (target.startswith("vm:") and target[3:] in d["exts"]))))
    if not ok:
        what = "an extension, ring group or IVR menu" + (" or business hours" if schedules else "")
        raise HTTPException(400, f"{target or 'That'} isn't {what}" + (", voicemail box or hang up." if extras else "."))
    return target


def needs_keypad(conn, target: str, tenant: Optional[int] = None) -> bool:
    """A call link reaching a menu, straight away or through business hours,
    shows a keypad."""
    tenant = current_tenant() if tenant is None else tenant
    menus = {r["number"] for r in conn.execute("SELECT number FROM ivr_menus WHERE tenant_id = ?", (tenant,))}
    h = conn.execute("SELECT open_target, closed_target FROM business_hours WHERE number = ? AND tenant_id = ?",
                     (target, tenant)).fetchone()
    return target in menus or bool(h and (h["open_target"] in menus or h["closed_target"] in menus))


def live_call_links(conn) -> List[sqlite3.Row]:
    """Enabled links (every tenant's) whose target still answers: an enabled
    extension, a ring group, an IVR menu or business hours of its tenant."""
    links = conn.execute("SELECT * FROM call_links WHERE enabled = 1 ORDER BY id").fetchall()
    d = {tid: destinations(conn, tid) for tid in {r["tenant_id"] for r in links}}
    return [r for r in links
            if not r["target"].startswith("vm:") and r["target"] != "hangup" and target_goto(r["target"], d[r["tenant_id"]])]


def web_guest_password(conn) -> str:
    r = conn.execute("SELECT value FROM settings WHERE key = 'webguest_password'").fetchone()
    if r:
        return r["value"]
    pw = secrets.token_urlsafe(18)
    _set_setting(conn, "webguest_password", pw)
    conn.commit()
    return pw


def _web_guest_endpoint(conn) -> List[str]:
    """The browsers' endpoint, written out in full (endpoint-ext is TLS and
    SDES, browsers need WebRTC's DTLS). No AOR: it can't register. Only
    there while at least one link is live."""
    links = live_call_links(conn)
    if not links:
        return []
    codecs = "alaw,ulaw" + (f",{VIDEO_CODECS}" if any(r["video"] for r in links) else "")
    return ["; Call links (browsers). The password is public by design: this",
            f"; endpoint can only reach [{WEBLINKS_CONTEXT}].",
            f"[{WEB_GUEST}]",
            "type=endpoint",
            f"context={WEBLINKS_CONTEXT}",
            f"message_context={WEBLINKS_CONTEXT}",
            "allow_subscribe=no",
            "allow_transfer=no",
            "webrtc=yes",
            "direct_media=no",
            "rewrite_contact=yes",
            "disallow=all",
            f"allow={codecs}",
            f"auth={WEB_GUEST}",
            'callerid="Web visitor" <web>',
            "",
            f"[{WEB_GUEST}]",
            "type=auth",
            "auth_type=userpass",
            f"username={WEB_GUEST}",
            f"password={web_guest_password(conn)}",
            ""]


def build_extensions_conf(conn) -> str:
    out = ["; Generated by the admin dashboard (Phone System tab).",
           "; Do not edit by hand: changes here are overwritten on the next save.", ""]
    settings = get_settings(conn)
    video = settings["video"] == "1"
    names = {t.id: t for t in all_tenant_names(conn)}
    for r in conn.execute("SELECT * FROM extensions WHERE enabled = 1 ORDER BY ext"):
        t = names[r["tenant_id"]]
        login = t.login(r["ext"])
        out += [f"[{login}](endpoint-ext)",
                f"auth={login}",
                f"aors={login}",
                f"callerid=\"{r['name']}\" <{r['ext']}>",
                f"mailboxes={r['ext']}@{t.vm}",
                f"subscribe_context={t.hints}",
                f"named_call_group={t.pickup}",
                f"named_pickup_group={t.pickup}",
                f"set_var=PBX_EXT={r['ext']}",
                f"set_var=PBX_TENANT={t.code}"]
        if t.id != MAIN_TENANT:
            out.append(f"context={t.internal}")  # the main tenant's come from the endpoint-ext template
            out.append(f"accountcode={t.code}")  # its calls' CDRs, for billing (the main tenant's: empty)
        if video:
            out.append(f"allow={VIDEO_CODECS}")  # added to the template's audio codecs
        if r["outbound_cid"]:
            # Its own outbound caller ID; [sub-outbound] and 112/999 fall
            # back to OUTBOUND_CID without it.
            out.append(f"set_var=EXT_CID={r['outbound_cid']}")
            if emergency_needs_main_number(settings, r["outbound_cid"]):
                out.append("set_var=EMERG_OWN_CID=0")  # Irish VoIP can't show it: 112/999 show the main number
        if r["vm_nopin"]:
            # Its own phones signed in with its password (TLS): *97 from
            # them needn't ask for the voicemail PIN. On the endpoint, so
            # nothing a caller sends can set it.
            out.append("set_var=VM_NOPIN=1")
        out += ["",
                f"[{login}](auth-ext)",
                f"username={login}",
                f"password={r['password']}",
                "",
                f"[{login}](aor-ext)",
                ""]
    out += _web_guest_endpoint(conn)
    return "\n".join(out)


# Carrier ceilings: the most calls at once each carrier allows (set to
# match its portal: its channels). A call out skips a carrier that's
# full as if it couldn't take the call, so it fails over to the next;
# when every carrier is full or down, the caller hears the engaged tone.
# A carrier's calls are counted on the carrier's own channel (Dial's
# pre-dial handler, [portal-trunkcount]), so a transferred call still
# counts while its carrier leg is up; incoming calls are counted from the
# trunk they arrive on ([portal-inbound]). 112/999 are counted, never
# refused. Nothing is generated for a carrier without a ceiling, so with
# none set the config is as it was.
TRUNK_MAX_LIMIT = 200


def _trunk_ceilings(s: Dict[str, str]) -> Dict[str, int]:
    return {t: int(s[f"trunk_max_{t}"]) for t in TRUNKS if (s.get(f"trunk_max_{t}") or "").isdigit()}


def _trunk_dial_opts(s: Dict[str, str], trunk: str) -> str:
    return f"b(portal-trunkcount^s^1({trunk}))" if trunk in _trunk_ceilings(s) else ""


def _trunk_guard(s: Dict[str, str], trunk: str, skip_to: str) -> List[str]:
    """Lines before a Dial on trunk: if it's full, skip to skip_to (the
    failover check) as if it couldn't take the call."""
    if trunk not in _trunk_ceilings(s):
        return []
    n = _trunk_ceilings(s)[trunk]
    return [f" same => n,Set(FULL=${{IF($[${{GROUP_COUNT({trunk}@trunkout)}} + ${{GROUP_COUNT({trunk}@trunkin)}} >= {n}]?1:0)}})",
            f' same => n,ExecIf($["${{FULL}}" = "1"]?NoOp({TRUNKS[trunk]["label"]} full at {n} calls at once))',
            ' same => n,ExecIf($["${FULL}" = "1"]?Set(DIALSTATUS=CHANUNAVAIL))',
            f' same => n,GotoIf($["${{FULL}}" = "1"]?{skip_to})']


def _trunkcount_context(s: Dict[str, str]) -> List[str]:
    if not _trunk_ceilings(s):
        return []
    return ["", "; ---- Carrier ceilings: counts each call on the carrier's own channel ----",
            "[portal-trunkcount]",
            "exten => s,1,Set(GROUP(trunkout)=${ARG1})",
            " same => n,Return()"]


def _all_tried(s: Dict[str, str]) -> List[str]:
    """With ceilings, every carrier full (or down) is a normal case: the
    engaged tone rather than just hanging up."""
    if not _trunk_ceilings(s):
        return []
    return [' same => n,GotoIf($["${DIALSTATUS}" = "CHANUNAVAIL" | "${DIALSTATUS}" = "CONGESTION"]?busy)']


# Caller-ID countries (decided 2026-11-12): carriers block calls showing
# their own country's numbers that arrive from abroad, so a caller ID has
# to leave through a carrier in its own country. Each route (cli_<key>, the
# key as for lcr_allow_) may list the calling codes of the caller IDs it can
# show, e.g. "353" or "61,353"; empty = any, as before. lcr.py only offers
# routes that can show the call's caller ID, else the tenant's main number
# (the answer then says |CLI:<number> and the dialplan switches to it).
# Nothing is generated for it while no route has a list.

def e164(number: str) -> str:
    """A caller ID as digits in international form: +353..., 00353... and
    Irish national numbers (01..., 087...) all become 353...; "" if it
    isn't a phone number. (So a number outside Ireland must be written in
    full, +61...: a national one is taken as Irish.)"""
    d = re.sub(r"[\s()-]", "", number or "")
    if d.startswith("+"):
        d = d[1:]
    elif d.startswith("00"):
        d = d[2:]
    elif d.startswith("0"):
        d = "353" + d[1:]
    return d if re.fullmatch(r"[1-9][0-9]{5,15}", d) else ""


def emergency_needs_main_number(s: Dict[str, str], cid: str) -> bool:
    """112/999 always go out on Irish VoIP: if it has a caller-ID list
    that this extension's own caller ID isn't on, its emergency calls show
    the tenant's main number instead (never refused)."""
    codes = cli_lists(s).get("irishvoip")
    shown = e164(cid)
    return bool(codes and shown and not any(shown.startswith(c) for c in codes))


def cli_lists(s: Dict[str, str]) -> Dict[str, List[str]]:
    lists = {}
    for t, r in ALL_ROUTES:
        value = (s.get(f"cli_{t}{r}") or "").strip()
        if value:
            lists[f"{t}{r}"] = [c for c in value.split(",") if c]
    return lists


def _route_check(kind: str, s: Optional[Dict[str, str]] = None) -> List[str]:
    """Asks lcr.py which routes to try (checked against the price cap,
    cheapest first where set), then tries them in order. Falls through to
    the lines after it (labelled nocheck) only if there's no answer."""
    s = s or {}
    vb_opts, iv_opts = _trunk_dial_opts(s, "voxbeam"), _trunk_dial_opts(s, "irishvoip")
    tried = "tried" if _trunk_ceilings(s) else "done"
    cli = bool(cli_lists(s))
    out = [" same => n,Set(CURLOPT(conntimeout)=1)",
           " same => n,Set(CURLOPT(httptimeout)=2)",
           " same => n,Set(CURLOPT(httpheader)=X-LCR-Token: ${LCR_TOKEN})",
           f" same => n,Set(LCR=${{CURL({LCR_URL}?kind={kind}&num=${{ARG2}}&tenant=${{OT}}"
           + ("&cli=${URIENCODE(${OUT_CID})}" if cli else "") + ")})",
           ' same => n,GotoIf($["${LCR:0:7}" = "REFUSE:"]?refused)',
           ' same => n,GotoIf($["${LCR:0:3}" != "OK:"]?nocheck)',
           " same => n,Set(ROUTES=${LCR:3})"]
    if cli:
        # OK:<routes>|CLI:<number>: no route can show the call's caller ID,
        # so show the tenant's main number instead.
        out += [" same => n,Set(NEWCID=${CUT(ROUTES,|,2)})",
                " same => n,Set(ROUTES=${CUT(ROUTES,|,1)})",
                ' same => n,ExecIf($["${NEWCID:0:4}" = "CLI:"]?Set(CALLERID(num)=${NEWCID:4}))',
                ' same => n,ExecIf($["${NEWCID:0:4}" = "CLI:"]?NoOp(No route can show the caller ID so the main number is shown))']
    for i in range(1, len(ALL_ROUTES) + 1):
        out += [f" same => n,Set(R=${{CUT(ROUTES,/,{i})}})",
                f' same => n,GotoIf($["${{R}}" = "irishvoip"]?iv{i})']
        out += [f' same => n,GotoIf($["${{R}}" = "{p}"]?vb{i})' for p in VOXBEAM_ROUTES]
        out += [f" same => n,Goto({tried})",  # empty (no more routes) or not a route we know
                f" same => n(vb{i}),NoOp()" if vb_opts else None,
                *_trunk_guard(s, "voxbeam", f"next{i}"),
                f" same => n{'' if vb_opts else f'(vb{i})'},Dial(PJSIP/${{R}}${{ARG2}}@voxbeam,60,{REC_DIAL}{vb_opts})",
                f" same => n,Goto(next{i})",
                f" same => n(iv{i}),NoOp()" if iv_opts else None,
                *_trunk_guard(s, "irishvoip", f"next{i}"),
                f" same => n{'' if iv_opts else f'(iv{i})'},Dial(PJSIP/${{ARG1}}@irishvoip,60,{REC_DIAL}{iv_opts})",
                f' same => n(next{i}),GotoIf($["${{DIALSTATUS}}" != "CHANUNAVAIL" & "${{DIALSTATUS}}" != "CONGESTION"]?done)']
        out = [line for line in out if line is not None]
    out += [f" same => n({tried}),NoOp()", *_all_tried(s)] if tried == "tried" else []
    return out + [" same => n,Goto(done)", " same => n(nocheck),NoOp(Route check didn't answer: ${LCR})"]


def _outbound_block(s: Dict[str, str], exten: str, trunks: List[str], refused: bool = False) -> List[str]:
    # The caller's tenant is its account code (as billing reads it): none
    # for the main tenant, whose limit counts its calls out only, as it
    # always has; any other tenant's calls in and out count together.
    # An account code that's no tenant's has no MAXCALLS_: its limit reads
    # as 0, so the call is refused, never let through uncounted.
    out = [f"exten => {exten},1,Set(OT=${{CHANNEL(accountcode)}})  ; the caller's tenant: empty for the main tenant",
           ' same => n,GotoIf($["${OT}" != ""]?tenant)',
           " same => n,Set(GROUP(outbound)=calls)",
           " same => n,GotoIf($[${GROUP_COUNT(calls@outbound)} > ${MAX_OUTBOUND}]?busy)",
           " same => n,Goto(counted)",
           " same => n(tenant),Set(GROUP(tenantcalls)=${OT})",
           " same => n,GotoIf($[${GROUP_COUNT(${OT}@tenantcalls)} > 0${MAXCALLS_${OT}}]?busy)",
           " same => n(counted),NoOp()",
           *REC_EXT_LINES,
           " same => n,Set(__RECTO=${ARG1})"]
    out += [' same => n,Set(OUT_CID=${IF($["${EXT_CID}" != ""]?${EXT_CID}:${IF($["${OT}" = ""]?${OUTBOUND_CID}:${OUTBOUND_CID_${OT}})})})',
            ' same => n,ExecIf($["${OUT_CID}" != ""]?Set(CALLERID(num)=${OUT_CID}))']
    if exten in NUMBER_TYPES and not refused:
        out += _route_check(exten, s)
        if exten == "international":
            # No answer means the cap can't be checked: refuse (2026-10-26).
            out += [" same => n,Goto(refused)"]
            trunks = []
    if refused:
        out += [" same => n,NoOp(These numbers are blocked)", " same => n,Goto(busy)"]
    elif not trunks and exten != "international":
        out += [" same => n,NoOp(No enabled trunk for these numbers)", " same => n,Goto(busy)"]
    for i, t in enumerate(trunks):
        out += _trunk_guard(s, t, f"tr{i}")
        out.append(f" same => n,Dial(PJSIP/{TRUNKS[t]['dial']}@{t},60,{REC_DIAL}{_trunk_dial_opts(s, t)})")
        label = f"(tr{i})" if t in _trunk_ceilings(s) else ""
        if i < len(trunks) - 1:
            # Only fall through to the next trunk if this one couldn't take
            # the call at all (down, refused, congested, or full here),
            # never after a normal answer/busy/no-answer.
            out.append(f' same => n{label},GotoIf($["${{DIALSTATUS}}" != "CHANUNAVAIL" & "${{DIALSTATUS}}" != "CONGESTION"]?done)')
        elif label:
            out.append(f" same => n{label},NoOp()")
    if trunks:
        out += _all_tried(s)
    return out + [" same => n(done),Hangup()",
                  " same => n(refused),NoOp(Refused: ${LCR})",
                  " same => n(busy),Congestion(5)",
                  " same => n,Hangup()"]


def build_voicemail_conf(conn) -> str:
    out = ["; Generated by the admin dashboard (Phone System tab).",
           "; Do not edit by hand: changes here are overwritten on the next save.",
           "; Included from voicemail.conf. mailbox => PIN,name[,email,,options]"]
    for t in all_tenant_names(conn):
        out += ["", f"[{t.vm}]"]
        for r in conn.execute("SELECT * FROM extensions WHERE enabled = 1 AND tenant_id = ? ORDER BY ext", (t.id,)):
            line = f"{r['ext']} => {r['vm_pin']},{r['name']}"
            if r["vm_email"]:
                line += f",{r['vm_email']},,attach=yes"
            out.append(line)
    return "\n".join(out) + "\n"


def _lcr_token(conn) -> str:
    """The shared secret Asterisk sends with route checks (made once)."""
    r = conn.execute("SELECT value FROM settings WHERE key = 'lcr_token'").fetchone()
    if r:
        return r["value"]
    token = secrets.token_hex(24)
    _set_setting(conn, "lcr_token", token)
    conn.commit()
    return token


def _call_ext_context(t: TenantNames) -> List[str]:
    """A call for an extension (from another extension, a ring of an inbound
    route, or a forward): forward always, else do not disturb counts as busy,
    else ring it; busy goes to the busy forward, anything else unanswered to
    the no-answer forward; with no forward, voicemail. Forwards to outside
    numbers go out through from-internal like a dialled call (price cap and
    all), billed to the forwarding extension (CDR userfield fwd:<ext>). No
    voicemail after that: an unanswered mobile keeps its own."""
    cf, cfb, cfu = (FORWARD_FAMILIES[k] for k in ("always", "busy", "no_answer"))
    me = f"{t.login_prefix}${{EXTEN}}"  # this extension's login: its endpoint and its AstDB key
    return [
        "; ---- Calls for an extension: do not disturb, forwarding, voicemail ----",
        f"[{t.call_ext}]",
        "exten => _2XX,1,Set(HOPS=$[0${HOPS} + 1])",
        f" same => n,GotoIf($[${{HOPS}} > {MAX_FORWARD_HOPS}]?vm-u)  ; forwarded round in a loop",
        f" same => n,Set(FWD=${{DB({cf}/{me})}})",
        ' same => n,GotoIf($["${FWD}" != ""]?fwd)',
        f' same => n,GotoIf($["${{DB({DND_FAMILY}/{me})}}" = "1"]?busy)',
        *REC_EXT_LINES,
        f" same => n,Dial(PJSIP/{me},30,{REC_DIAL})",
        ' same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?done)',
        ' same => n,GotoIf($["${DIALSTATUS}" = "BUSY"]?busy)',
        f" same => n,Set(FWD=${{DB({cfu}/{me})}})",
        ' same => n,GotoIf($["${FWD}" != ""]?fwd)',
        f" same => n(vm-u),VoiceMail(${{EXTEN}}@{t.vm},u)",
        " same => n,Hangup()",
        f" same => n(busy),Set(FWD=${{DB({cfb}/{me})}})",
        ' same => n,GotoIf($["${FWD}" != ""]?fwd)',
        f" same => n,VoiceMail(${{EXTEN}}@{t.vm},b)",
        " same => n,Hangup()",
        ' same => n(fwd),GotoIf($["${FWD}" = "${EXTEN}"]?vm-u)',
        f' same => n,GotoIf($[${{REGEX("{FORWARD_TARGET_DIALPLAN_RE}" ${{FWD}})}}]?:vm-u)  ; set by hand to somewhere not allowed',
        ' same => n,ExecIf($["${CDR(userfield)}" = ""]?Set(CDR(userfield)=fwd:${EXTEN}))',
        f" same => n,Set(EXT_CID=${{EXTCID_{me}}})",
        f" same => n,Goto({t.dial_from},${{FWD}},1)",
        " same => n(done),Hangup()",
    ]


# Who's calling, for recordings: the extension that started the call and
# its tenant, kept on every channel the call creates (__ = inherited);
# wherever RECEXT is set, RECTENANT is too. Click to call sets both in its
# tenant's c2c context (its dialling leg isn't an extension's channel).
REC_EXT_LINES = [' same => n,ExecIf($["${RECEXT}" = "" & "${PBX_EXT}" != ""]?Set(__RECEXT=${PBX_EXT}))',
                 ' same => n,ExecIf($["${RECTENANT}" = "" & "${PBX_TENANT}" != ""]?Set(__RECTENANT=${PBX_TENANT}))']
REC_DIAL = "U(portal-rec)"  # Dial option: runs [portal-rec] on the answering channel


def _rec_context() -> List[str]:
    """Run by Dial (option U) on the channel that answers, before the two
    are connected. An extension answering: recorded if it, or the
    extension calling it, records everything. An outside number answering
    (a call out): recorded if the calling extension records, and then that
    person hears the notice first. MixMonitor's b: only while connected.
    Shared by every tenant: the call's tenant (the answering extension's,
    else the caller's) picks the REC_ flags (by login: LOGINPRE_), the
    folder (RECDIR_) and the notice (RECNOTICE_) from [globals]. A call
    whose tenant can't be told isn't recorded, never filed as another's."""
    tz = PBX_TIMEZONE
    return ["; ---- Call recording: which answered calls are recorded ----",
            "[portal-rec]",
            "exten => s,1,Set(CALLEE=${PBX_EXT})  ; empty unless an extension answered",
            ' same => n,Set(RT=${IF($["${CALLEE}" != ""]?${PBX_TENANT}:${RECTENANT})})  ; the call\'s tenant',
            ' same => n,GotoIf($["${RT}" = ""]?done)',
            " same => n,Set(RPRE=${LOGINPRE_${RT}})",
            ' same => n,GotoIf($["${CALLEE}" != ""]?toext)',
            ' same => n,GotoIf($["${RECEXT}" = "" | "${REC_${RPRE}${RECEXT}}" = ""]?done)',
            " same => n,Set(RX=${RECEXT})",
            " same => n,Set(RO=${RECTO})",
            " same => n,Set(RD=out)",
            ' same => n,ExecIf($["${RECNOTICE_${RT}}" != ""]?Playback(${RECNOTICE_${RT}}))  ; told before they\'re connected',
            " same => n,Goto(rec)",
            ' same => n(toext),GotoIf($["${REC_${RPRE}${CALLEE}}${REC_${RPRE}${RECEXT}}" = ""]?done)',
            " same => n,Set(RX=${CALLEE})",
            " same => n,Set(RO=${CALLERID(num)})",
            # Recorded for the extension calling, not this one: file it under that one.
            ' same => n,ExecIf($["${REC_${RPRE}${CALLEE}}" = ""]?Set(RX=${RECEXT}))',
            ' same => n,ExecIf($["${REC_${RPRE}${CALLEE}}" = ""]?Set(RO=${CALLEE}))',
            ' same => n,Set(RD=${IF($["${RECEXT}" = ""]?in:int)})',
            " same => n(rec),Set(RO=${FILTER(0-9,${RO})})",
            ' same => n,ExecIf($["${RO}" = ""]?Set(RO=unknown))',
            f" same => n,MixMonitor(${{RECDIR_${{RT}}}}/${{STRFTIME(${{EPOCH}},{tz},%Y%m%d-%H%M%S)}}_${{RX}}_${{RO:0:20}}_${{RD}}_${{UNIQUEID}}.ulaw,b)",
            " same => n(done),Return()"]


def _features_context(t: TenantNames) -> List[str]:
    """Feature codes, dialled from an extension. ME is the extension that
    dialled, from its endpoint (PBX_EXT), never the caller ID, which a
    phone could set to anything."""
    out = ["; ---- Feature codes (included into from-internal) ----",
           "; *78/*79 do not disturb on/off; *72<number>/*73 forward always;",
           "; *90<number>/*91 forward when busy; *52<number>/*53 forward when",
           "; unanswered; *8 answer any ringing extension; **2XX answer that one;",
           f"; {PARK_EXT} park (transfer a call to it), {PARK_SLOTS[0]}-{PARK_SLOTS[-1]} pick a parked call up.",
           f"[{t.features}]",
           # Phones only: browsers and menus never reach this context.
           f"include => {t.park_context}"]
    db_key = f"{t.login_prefix}${{ME}}"  # the dialling extension's login: its AstDB key
    me = [" same => n,Set(ME=${PBX_EXT})",
          f' same => n,GotoIf($[${{REGEX("^2[0-9][0-9]$" ${{ME}})}}]?:bad)']
    end = [" same => n,Hangup()", " same => n(bad),Playback(invalid)", " same => n,Hangup()"]
    out += ["exten => *78,1,Answer()"] + me + [
        f" same => n,Set(DB({DND_FAMILY}/{db_key})=1)",
        " same => n,Playback(do-not-disturb&activated)"] + end
    out += ["exten => *79,1,Answer()"] + me + [
        f" same => n,NoOp(${{DB_DELETE({DND_FAMILY}/{db_key})}})",
        " same => n,Playback(do-not-disturb&de-activated)"] + end
    for on, off, key, sound in (("*72", "*73", "always", "call-fwd-unconditional"),
                                ("*90", "*91", "busy", "call-fwd-on-busy"),
                                ("*52", "*53", "no_answer", "call-fwd-no-ans")):
        fam = FORWARD_FAMILIES[key]
        out += [f"exten => _{on}X.,1,Answer()"] + me + [
            " same => n,Set(T=${EXTEN:3})",
            f' same => n,GotoIf($[${{REGEX("{FORWARD_TARGET_DIALPLAN_RE}" ${{T}})}}]?:bad)',
            ' same => n,GotoIf($["${T}" = "${ME}"]?bad)',
            f" same => n,Set(DB({fam}/{db_key})=${{T}})",
            f" same => n,Playback({sound}&activated)",
            " same => n,SayDigits(${T})"] + end
        out += [f"exten => {off},1,Answer()"] + me + [
            f" same => n,NoOp(${{DB_DELETE({fam}/{db_key})}})",
            f" same => n,Playback({sound}&de-activated)"] + end
    out += ["exten => *8,1,Pickup()", " same => n,Hangup()",
            f"exten => _**2XX,1,PickupChan(PJSIP/{t.login_prefix}${{EXTEN:2}}-,p)", " same => n,Hangup()"]
    return out


def _weblinks_context(conn, notices: Dict[int, bool]) -> List[str]:
    """One entry per live call link, wl-<token>; anything else a browser
    asks for doesn't exist here (404). Limits: calls at once per link and
    in all, and the link's maximum length. The visitor's name is filtered
    and cut short before it's shown on the phone. Each link reaches only its
    own tenant's targets, and plays that tenant's notice (notices)."""
    out = ["; ---- Call links: what browsers (endpoint webguest) can reach, nothing else ----",
           f"[{WEBLINKS_CONTEXT}]"]
    names = {t.id: t for t in all_tenant_names(conn)}
    total = int(get_settings(conn)["web_max_total"])
    for r in live_call_links(conn):
        t = names[r["tenant_id"]]
        per_tenant = int(get_settings(conn, t.id)["web_max"])
        target = target_goto(r["target"], destinations(conn, t.id), t)
        label = re.sub(r"[^A-Za-z0-9 .'_-]", "", r["label"])[:30]
        out += [f"exten => wl-{r['token']},1,NoOp(Call link {r['id']}: {label})",
                f" same => n,Set(GROUP(weblink)=link{r['id']})",
                f" same => n,GotoIf($[${{GROUP_COUNT(link{r['id']}@weblink)}} > {WEBLINK_MAX_PER_LINK}]?busy)",
                f" same => n,Set(GROUP(webtenant)={t.code})",
                f" same => n,GotoIf($[${{GROUP_COUNT({t.code}@webtenant)}} > {per_tenant}]?busy)",
                " same => n,Set(GROUP(webguests)=all)",
                f" same => n,GotoIf($[${{GROUP_COUNT(all@webguests)}} > {total}]?busy)",
                f" same => n,Set(TIMEOUT(absolute)={int(r['max_minutes']) * 60})",
                # The name the visitor typed comes in its own header (the
                # endpoint's fixed caller ID would hide it), filtered and cut.
                " same => n,Set(WNAME=${FILTER(A-Za-z0-9 ._,${PJSIP_HEADER(read,X-Web-Name)})})",
                f" same => n,Set(CALLERID(name)=${{IF($[\"${{WNAME}}\" = \"\"]?Web visitor:${{WNAME:0:24}})}} ({label}))",
                " same => n,Set(CALLERID(num)=web)",
                *t.accountcode(),
                *([f" same => n,Playback({t.notice[:-len('.ulaw')]})  ; calls may be recorded"] if notices[t.id] else []),
                f" same => n,{target}",
                " same => n(busy),Congestion(5)",
                " same => n,Hangup()"]
    return out


def _ivr_contexts(conn, t: TenantNames) -> List[str]:
    """One context per menu, [portal-ivr-5XX], entered at s: the greeting,
    then wait for a key. No includes, ever: browsers (call links) reach
    these too, so a menu must offer only what's listed in it. A wrong key or
    no key repeats the greeting, then the fallback (or goodbye). Optional
    direct dial: any switched-on extension, by its number (a key 2 then
    waits a few seconds for more digits first)."""
    d = destinations(conn, t.id)
    out = []
    for m in conn.execute("SELECT * FROM ivr_menus WHERE tenant_id = ? ORDER BY number", (t.id,)):
        n = m["number"]
        greeting = greeting_path(n, t)
        fallback = target_goto(m["fallback"], d, t) if m["fallback"] else None
        give_up = [f" same => n,{fallback}"] if fallback else [" same => n,Playback(vm-goodbye)", " same => n,Hangup()"]
        label = re.sub(r"[^A-Za-z0-9 .'_-]", "", m["name"])
        out += ["", f"[{t.ivr(n)}]",
                f"exten => s,1,NoOp(IVR menu {n}: {label})",
                " same => n,Set(MENUHOPS=$[0${MENUHOPS} + 1])",
                f" same => n,GotoIf($[${{MENUHOPS}} > {MAX_MENU_HOPS}]?bye)",
                ' same => n,ExecIf($["${CHANNEL(state)}" != "Up"]?Answer())',
                " same => n,ExecIf($[${MENUHOPS} = 1]?Wait(1))  ; so the greeting's start isn't cut off",
                " same => n,Set(TRIES=0)",
                " same => n,Set(TIMEOUT(digit)=3)",
                " same => n(menu),Set(TRIES=$[${TRIES} + 1])",
                (f" same => n,Background({greeting[:-len('.ulaw')]})" if os.path.exists(greeting)
                 else " same => n,Background(beep)  ; no greeting yet"),
                f" same => n,WaitExten({int(m['timeout'])})",
                " same => n(bye),Hangup()"]
        for key, target in sorted(menu_options(m).items()):
            goto = target_goto(target, d, t)
            if goto:
                out += [f"exten => {key},1,{goto}"] + ([] if goto == "Hangup()" else [" same => n,Hangup()"])
        if m["direct_dial"]:
            # A key 2 still works: Asterisk waits TIMEOUT(digit) for more.
            out += [f"exten => {e},1,Goto({t.call_ext},{e},1)" for e in sorted(d["active"])]
        retry = f" same => n,GotoIf($[${{TRIES}} < {int(m['retries'])}]?s,menu)"
        out += ["exten => t,1,NoOp(No key pressed)", retry, *give_up,
                "exten => i,1,Playback(invalid)", retry, *give_up]
    return out


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # %u: 1 = Monday


def hours_week(row) -> Dict[str, list]:
    try:
        week = json.loads(row["week"] or "{}")
        return {day: [list(r) for r in week.get(day, [])] for day in DAYS}
    except (ValueError, TypeError, AttributeError):
        return {day: [] for day in DAYS}


def hours_holidays(row) -> List[str]:
    try:
        return [h for h in json.loads(row["holidays"] or "[]") if isinstance(h, str)]
    except ValueError:
        return []


def _hhmm(t: str) -> int:
    return int(t[:2]) * 100 + int(t[3:])


def hours_open_now(row, now: Optional[datetime] = None) -> bool:
    """Same rule as the dialplan: the override, then holidays (closed all
    day), then the day's ranges, start included, end not."""
    from zoneinfo import ZoneInfo
    if row["override"] in ("open", "closed"):
        return row["override"] == "open"
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(PBX_TIMEZONE))
    if now.strftime("%Y-%m-%d") in hours_holidays(row):
        return False
    t = now.hour * 100 + now.minute
    return any(_hhmm(a) <= t < _hhmm(b) for a, b in hours_week(row)[DAYS[now.weekday()]])


def _hours_contexts(conn, t: TenantNames) -> List[str]:
    """One context per schedule, [portal-hours-4XX], entered at s: open or
    closed by the time in PBX_TIMEZONE, as day + time (%u%H%M: Monday 9:30
    is 10930), compared as numbers, so the dashboard's open/closed badge
    uses exactly the same rule. No includes (browsers reach these too). A
    target that's gone hands over to the other one."""
    d = destinations(conn, t.id)
    tz = PBX_TIMEZONE
    out = []
    for h in conn.execute("SELECT * FROM business_hours WHERE tenant_id = ? ORDER BY number", (t.id,)):
        n = h["number"]
        label = re.sub(r"[^A-Za-z0-9 .'_-]", "", h["name"])
        open_goto, closed_goto = target_goto(h["open_target"], d, t), target_goto(h["closed_target"], d, t)
        open_goto, closed_goto = open_goto or closed_goto or "Hangup()", closed_goto or open_goto or "Hangup()"
        out += ["", f"[{t.hours(n)}]",
                f"exten => s,1,NoOp(Business hours {n}: {label})"]
        if h["override"] in ("open", "closed"):
            out.append(f" same => n,Goto({h['override']})  ; set by hand in the dashboard")
        else:
            out += [f" same => n,Set(HDATE=${{STRFTIME(${{EPOCH}},{tz},%Y-%m-%d)}})",
                    f" same => n,Set(HNOW=${{STRFTIME(${{EPOCH}},{tz},%u%H%M)}})"]
            out += [f' same => n,GotoIf($["${{HDATE}}" = "{day}"]?closed)  ; holiday' for day in hours_holidays(h)]
            for i, day in enumerate(DAYS, start=1):
                out += [f" same => n,GotoIf($[${{HNOW}} >= {i}{_hhmm(a):04d} & ${{HNOW}} < {i}{_hhmm(b):04d}]?open)"
                        f"  ; {day} {a}-{b}" for a, b in hours_week(h)[day]]
        out += [f" same => n(closed),{closed_goto}", " same => n,Hangup()",
                f" same => n(open),{open_goto}", " same => n,Hangup()"]
    return out


def _click_to_call_contexts(t: TenantNames) -> List[str]:
    """Click to call: the dashboard originates Local/<ext><number>@ring,
    which rings the extension showing the number; once it answers, the
    other half dials the number from dial as that extension (its caller ID,
    billed to it via CDR userfield c2c:<ext>) through from-internal, so the
    route check, price cap and call limit apply as for a dialled call.
    Extensions are always 3 digits. Not included anywhere a phone can
    reach, so a phone can't use these to pass as another extension. The
    ring leg skips do not disturb and forwarding: the user asked for it.
    One pair per tenant, ringing its own logins and dialling out from its
    own internal context."""
    pre = t.login_prefix
    return ["; ---- Click to call: only started by the dashboard, never dialled ----",
            f"[{t.c2c_ring}]",
            "exten => _2XXX.,1,Set(CALLERID(name)=Calling ${EXTEN:3})",
            " same => n,Set(CALLERID(num)=${EXTEN:3})",
            f" same => n,Dial(PJSIP/{pre}${{EXTEN:0:3}},30)",
            " same => n,Hangup()",
            f"[{t.c2c_dial}]",
            "exten => _2XXX.,1,Set(C2C=${EXTEN:0:3})",
            " same => n,Set(CDR(userfield)=c2c:${C2C})",
            *t.accountcode(),
            " same => n,Set(__RECEXT=${C2C})",
            f" same => n,Set(__RECTENANT={t.code})",
            f" same => n,Set(EXT_CID=${{EXTCID_{pre}${{C2C}}}})",
            f" same => n,Goto({t.dial_from},${{EXTEN:3}},1)"]


# What a phone can dial out to, shared by every tenant: numbers sorted by
# kind, then [sub-outbound] (trunks, price cap, call limit). Moved here from
# the hand-written [from-internal], unchanged. Anything not matched (premium
# 15xx, short codes) is refused.
OUTBOUND_PATTERNS = [
    "; ---- Outside numbers, by kind (included into every tenant's internal context) ----",
    "; ARG1 = national format, ARG2 = international without 00.",
    "[portal-outbound]",
    "; Irish mobiles: 083, 085, 086, 087, 089 + 7 digits",
    "exten => _08[35679]XXXXXXX,1,Gosub(sub-outbound,mobile,1(${EXTEN},353${EXTEN:1}))",
    "; Other Irish numbers: 0 + 8 or 9 digits (01 234 5678, 021 123 4567, 0818...)",
    "exten => _0[1-9]XXXXXXX,1,Gosub(sub-outbound,landline,1(${EXTEN},353${EXTEN:1}))",
    "exten => _0[1-9]XXXXXXXX,1,Gosub(sub-outbound,landline,1(${EXTEN},353${EXTEN:1}))",
    "; Irish freephone 1800",
    "exten => _1800XXXXXX,1,Gosub(sub-outbound,freephone,1(${EXTEN},353${EXTEN}))",
    "; Irish numbers dialled in international form go the same way as above",
    "exten => _00353X.,1,Goto(0${EXTEN:5},1)",
    "exten => _+353X.,1,Goto(0${EXTEN:4},1)",
    "exten => _003531800XXXXXX,1,Goto(1800${EXTEN:9},1)",
    "exten => _+3531800XXXXXX,1,Goto(1800${EXTEN:8},1)",
    "; +353 (0)87... as phones often store it: drop the extra 0",
    "exten => _003530X.,1,Goto(0${EXTEN:6},1)",
    "exten => _+3530X.,1,Goto(0${EXTEN:5},1)",
    "; International (outside Ireland): 00 or + then at least 7 digits",
    "exten => _00[1-9]XXXXXX.,1,Gosub(sub-outbound,international,1(${EXTEN},${EXTEN:2}))",
    "exten => _+[1-9]XXXXXX.,1,Gosub(sub-outbound,international,1(00${EXTEN:1},${EXTEN:1}))",
]


def _internal_context(t: TenantNames, outbound_cid: str, emergency_opts: str = "", emergency_check: bool = False,
                      vm_nopin: bool = False) -> List[str]:
    """Everything a tenant's phone can dial: its own extensions, groups,
    menus, schedules and feature codes, voicemail, 112/999 and outside
    numbers. Other tenants' phones start here (endpoint context); the main
    tenant's from [from-internal], which includes this from step 2.5.
    Emergency calls go straight out via Irish VoIP, never subject to the
    call limit, showing the extension's own caller ID, else the tenant's,
    else the server's."""
    fallback = outbound_cid or "${OUTBOUND_CID}"
    if t.id == MAIN_TENANT:
        fallback = "${OUTBOUND_CID}"  # the main tenant's is the server's
    out = [f"; ---- Calls placed by tenant {t.code}'s phones ----",
           f"[{t.internal}]",
           f"include => {t.ringgroups}",
           f"include => {t.features}",
           "include => portal-outbound",
           f"exten => _2XX,1,Goto({t.call_ext},${{EXTEN}},1)",
           # Its own mailbox by the endpoint, not the caller ID a phone sends;
           # without the PIN from its own phones if it chose that (VM_NOPIN
           # on the endpoint).
           *([f'exten => *97,1,GotoIf($["${{VM_NOPIN}}" = "1"]?nopin)',
              f" same => n,VoiceMailMain(${{PBX_EXT}}@{t.vm})",
              " same => n,Hangup()",
              f" same => n(nopin),VoiceMailMain(${{PBX_EXT}}@{t.vm},s)"] if vm_nopin
             else [f"exten => *97,1,VoiceMailMain(${{PBX_EXT}}@{t.vm})"]),
           " same => n,Hangup()",
           f"exten => *98,1,VoiceMailMain(@{t.vm})",
           " same => n,Hangup()"]
    for n in ("112", "999"):
        # With caller-ID lists, an extension whose own caller ID Irish VoIP
        # can't show (EMERG_OWN_CID=0 on its endpoint) shows the main number.
        own = '"${EXT_CID}" != "" & "${EMERG_OWN_CID}" != "0"' if emergency_check else '"${EXT_CID}" != ""'
        out += [f'exten => {n},1,Set(OUT_CID=${{IF($[{own}]?${{EXT_CID}}:{fallback})}})',
                ' same => n,ExecIf($["${OUT_CID}" != ""]?Set(CALLERID(num)=${OUT_CID}))',
                f" same => n,Dial(PJSIP/{n}@irishvoip,120{',' + emergency_opts if emergency_opts else ''})",  # counted, never refused
                " same => n,Hangup()"]
    return out


def _count_inbound(t: TenantNames) -> List[str]:
    """An incoming call counts towards the tenant's limit (calls in and out
    together); one over it goes to the tenant's main mailbox. Not for the
    main tenant, whose limit counts its calls out only."""
    if t.id == MAIN_TENANT:
        return []
    return [f" same => n,Set(GROUP(tenantcalls)={t.code})",
            f" same => n,ExecIf($[${{GROUP_COUNT({t.code}@tenantcalls)}} > ${{MAXCALLS_{t.code}}}]?Goto({t.prefix}-full,s,1))"]


def _full_context(conn, t: TenantNames) -> List[str]:
    """Where an incoming call over the tenant's limit goes: its main mailbox
    (the setting, else its lowest switched-on extension), else busy."""
    active = destinations(conn, t.id)["active"]
    box = get_settings(conn, t.id)["main_mailbox"]
    box = box if box in active else (min(active) if active else "")
    return [f"; ---- Tenant {t.code}: incoming calls over its limit ----",
            f"[{t.prefix}-full]",
            "exten => s,1,Answer()",
            (f" same => n,VoiceMail({box}@{t.vm},u)" if box else " same => n,Busy(5)"),
            " same => n,Hangup()"]


def _ringgroups_context(conn, t: TenantNames) -> List[str]:
    """The tenant's ring groups, and its IVR menus and schedules by number
    (so phones and inbound routes dial them like a group)."""
    active = destinations(conn, t.id)["active"]
    out = ["",
           "; ---- Ring groups (included into from-internal) ----",
           f"[{t.ringgroups}]"]
    for g in conn.execute("SELECT * FROM ring_groups WHERE tenant_id = ? ORDER BY number", (t.id,)):
        members = [m for m in _members(g) if m in active]
        n, ring = g["number"], g["ring_time"]
        out.append(f"exten => {n},1,NoOp(Ring group {n}: {g['name']})")
        out += REC_EXT_LINES
        # Members on do not disturb are left out, at the time of the call.
        if not members:
            out.append(" same => n,NoOp(No active members)")
        elif g["strategy"] == "hunt":
            for i, m in enumerate(members):
                out += [f' same => n,GotoIf($["${{DB({DND_FAMILY}/{t.login(m)})}}" = "1"]?skip{i})',
                        f" same => n,Dial(PJSIP/{t.login(m)},{ring},{REC_DIAL})",
                        ' same => n,ExecIf($["${DIALSTATUS}" = "ANSWER"]?Hangup())',
                        f" same => n(skip{i}),NoOp()"]
        else:
            out.append(" same => n,Set(RG=)")
            out += [f' same => n,ExecIf($["${{DB({DND_FAMILY}/{t.login(m)})}}" != "1"]?Set(RG=${{RG}}&PJSIP/{t.login(m)}))'
                    for m in members]
            out += [' same => n,GotoIf($["${RG}" = ""]?nobody)',
                    f" same => n,Dial(${{RG:1}},{ring},{REC_DIAL})",
                    ' same => n,ExecIf($["${DIALSTATUS}" = "ANSWER"]?Hangup())',
                    " same => n(nobody),NoOp()"]
        if g["voicemail"] in active:
            out.append(f" same => n,VoiceMail({g['voicemail']}@{t.vm},u)")
        out.append(" same => n,Hangup()")
    # IVR menus are dialled like ring groups (phones, inbound routes): same
    # included context, so the static extensions.conf needs no change.
    out += [f"exten => {r['number']},1,Goto({t.ivr(r['number'])},s,1)"
            for r in conn.execute("SELECT number FROM ivr_menus WHERE tenant_id = ? ORDER BY number", (t.id,))]
    out += [f"exten => {r['number']},1,Goto({t.hours(r['number'])},s,1)"
            for r in conn.execute("SELECT number FROM business_hours WHERE tenant_id = ? ORDER BY number", (t.id,))]
    return out


def build_portal_dialplan(conn) -> str:
    s = get_settings(conn)
    tenants = all_tenant_names(conn)

    out = ["; Generated by the admin dashboard (Phone System tab).",
           "; Do not edit by hand: changes here are overwritten on the next save.",
           "; Included from extensions.conf.", "",
           "[globals]",
           f"OUTBOUND_CID={s['outbound_cid']}",
           f"MAX_OUTBOUND={s['max_outbound']}",
           f"VOXBEAM_PREFIX={s['voxbeam_prefix']}",
           f"LCR_TOKEN={_lcr_token(conn)}"]
    # Each extension's own caller ID, for calls it forwards to outside numbers.
    names = {t.id: t for t in tenants}
    out += [f"EXTCID_{names[r['tenant_id']].login(r['ext'])}={r['outbound_cid']}"
            for r in conn.execute("SELECT ext, outbound_cid, tenant_id FROM extensions WHERE enabled = 1 AND outbound_cid != '' "
                                  "ORDER BY ext")]
    # Extensions whose calls are all recorded (see [portal-rec]).
    out += [f"REC_{names[r['tenant_id']].login(r['ext'])}=1"
            for r in conn.execute("SELECT ext, tenant_id FROM extensions WHERE enabled = 1 AND record = 1 ORDER BY ext")]
    # Each tenant's, for [portal-rec] (shared): how its logins start (none
    # for the main tenant's, still the bare extension), where its
    # recordings go, and its notice, while any of its extensions records
    # everything.
    notices = {t.id: os.path.exists(t.notice) and bool(conn.execute(
        "SELECT 1 FROM extensions WHERE enabled = 1 AND record = 1 AND tenant_id = ?", (t.id,)).fetchone())
        for t in tenants}
    for t in tenants:
        if t.id != MAIN_TENANT:
            ts = get_settings(conn, t.id)
            # Calls in and out together; a suspended tenant's 0: none out, every
            # call in to its main mailbox (112/999 don't pass the limit).
            suspended = conn.execute("SELECT status FROM tenants WHERE id = ?", (t.id,)).fetchone()["status"] != "active"
            out += [f"MAXCALLS_{t.code}={0 if suspended else ts['max_outbound']}"]
            out += [f"OUTBOUND_CID_{t.code}={ts['outbound_cid']}"] if ts["outbound_cid"] else []
        out += [f"LOGINPRE_{t.code}={t.login_prefix}"] if t.login_prefix else []
        out += [f"RECDIR_{t.code}={t.rec_dir}"]
        out += [f"RECNOTICE_{t.code}={t.notice[:-len('.ulaw')]}"] if notices[t.id] else []
    # Per tenant from here on: each section once for every tenant, with its
    # own names (TenantNames), reaching only its own extensions.
    for t in tenants:
        out += ["",
                "; ---- Busy lamps (BLF) and presence: one hint per extension ----",
                f"[{t.hints}]"]
        out += [f"exten => {e},hint,PJSIP/{t.login(e)}" for e in sorted(destinations(conn, t.id)["active"])]
        out += [f"exten => {n},hint,park:{n}@{t.park_lot}" for n in PARK_SLOTS]  # lit while a call is parked there
    for t in tenants:
        out += [""] + _call_ext_context(t)
    for t in tenants:
        out += [""] + _features_context(t)
    for t in tenants:
        out += [""] + _click_to_call_contexts(t)
    out += [""] + _weblinks_context(conn, notices)
    out += [""] + _rec_context()
    for t in tenants:
        out += _ringgroups_context(conn, t) + _ivr_contexts(conn, t) + _hours_contexts(conn, t)
    for t in tenants:
        nopin = bool(conn.execute("SELECT 1 FROM extensions WHERE tenant_id = ? AND enabled = 1 AND vm_nopin = 1",
                                  (t.id,)).fetchone())
        out += [""] + _internal_context(t, get_settings(conn, t.id)["outbound_cid"], _trunk_dial_opts(s, "irishvoip"),
                                        bool(cli_lists(s).get("irishvoip")), nopin)
        if t.id != MAIN_TENANT:
            out += [""] + _full_context(conn, t)
    out += [""] + OUTBOUND_PATTERNS
    out += _trunkcount_context(s)

    enabled = [t for t in s["trunk_order"].split(",") if t in TRUNKS and s.get(f"trunk_{t}_enabled") == "1"]
    out += ["", "; ---- Outbound: one extension per kind of number ----",
            "; ARG1 = national number, ARG2 = international (353..., 44...)",
            "; s = the trunk order (kept for dialplans that don't say the kind)",
            "[sub-outbound]"]
    out += _outbound_block(s, "s", enabled)
    for kind in NUMBER_TYPES:
        route = s.get(f"route_{kind}", "default")
        trunks = enabled if route in ("default", "cheapest") else [t for t in enabled if t == route]
        out += [""] + _outbound_block(s, kind, trunks, refused=route == "off")
    out += ["",
            "; ---- Inbound: DID-specific routes, then the default ----",
            "[portal-inbound]"]
    # Outside callers hear the recording notice first, while any of the
    # number's tenant's extensions records everything. The main tenant's
    # numbers go through from-internal as before; any other tenant's
    # straight to its own target (from-internal is the main tenant's).
    told = {t.id: [f" same => n,Playback({t.notice[:-len('.ulaw')]})"] if notices[t.id] else [] for t in tenants}
    # With carrier ceilings, an incoming call counts against the trunk it
    # came in on (the channel is the carrier's).
    trunkin = [" same => n,Set(GROUP(trunkin)=${CHANNEL(endpoint)})"] if _trunk_ceilings(s) else []
    names = {t.id: t for t in tenants}
    for r in conn.execute("SELECT * FROM inbound_routes ORDER BY did"):
        tid = r["tenant_id"]
        goto = (f"Goto(from-internal,{r['target']},1)" if tid == MAIN_TENANT
                else target_goto(r["target"], destinations(conn, tid), names[tid]) or "Hangup()")
        out += [f"exten => {r['did']},1,NoOp(Inbound {r['did']})", *trunkin, *names[tid].accountcode(),
                *_count_inbound(names[tid]), *told[tid], f" same => n,{goto}"]
    default = s["inbound_default"]  # numbers not listed are the main tenant's
    for x in ("_X.", "s"):
        out += [f"exten => {x},1,NoOp(Inbound)", *trunkin, *told[MAIN_TENANT], f" same => n,Goto(from-internal,{default},1)"]
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


def build_parking_conf(conn) -> str:
    """One lot per tenant other than the main one, like Asterisk's default
    lot (park on 700, slots 701-720, 45 s, then back to the phone that
    parked it), in the tenant's own context, which only its feature codes
    include."""
    out = ["; Generated by the admin dashboard (Phone System tab).",
           "; Do not edit by hand: changes here are overwritten on the next save.",
           "; Included from res_parking.conf. The main tenant uses Asterisk's own [default] lot."]
    for t in all_tenant_names(conn):
        if t.id == MAIN_TENANT:
            continue
        out += ["", f"[{t.park_lot}]",
                f"parkext => {PARK_EXT}",
                f"parkpos => {PARK_SLOTS[0]}-{PARK_SLOTS[-1]}",
                f"context => {t.park_context}",
                "parkingtime => 45"]
    return "\n".join(out) + "\n"


def generate_config(conn) -> Dict[str, str]:
    """Every file the dashboard generates for Asterisk, by path, in the
    order they're written."""
    return {EXT_CONF: build_extensions_conf(conn),
            PORTAL_DIALPLAN: build_portal_dialplan(conn),
            VOICEMAIL_CONF: build_voicemail_conf(conn),
            PARKING_CONF: build_parking_conf(conn)}


def _unchanged(path: str, content: str) -> bool:
    try:
        with open(path) as f:
            return f.read() == content
    except OSError:
        return False


def _deliver_local(files: Dict[str, str]) -> Tuple[bool, List[str]]:
    """This server: write the files, then reload what reads them."""
    messages, ok = [], True
    # Parking is reloaded only when its lots change, never on an everyday
    # save: calls may be parked.
    parking_changed = PARKING_CONF in files and not _unchanged(PARKING_CONF, files[PARKING_CONF])
    try:
        for path, content in files.items():
            _write_conf(path, content)
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
        if parking_changed:
            out = asterisk_cli("module reload res_parking.so")
            if out is None or "No such" in out or "not found" in out.lower():
                messages.append("Parking didn't reload, so a new tenant's park lot isn't there yet "
                                "(README, \"Call park\").")
    return ok, messages


# Where a tenant's config goes, by tenants.node: "" is this server, the
# only one so far. Another Asterisk server (or the standby) is another
# entry here that sends the same files over and reloads them there.
NODES = {"": _deliver_local}


def apply_to_node(node: str, files: Dict[str, str]) -> Tuple[bool, List[str]]:
    deliver = NODES.get(node)
    if deliver is None:
        return False, [f"Saved, but there's no Asterisk server called {node!r} to apply it to."]
    return deliver(files)


GREETING_FILE_RE = re.compile(r"(ivr-[0-9]{3}|recording-notice)\.ulaw")


def move_main_recordings() -> int:
    """The main tenant's recordings from before it had a folder of its own
    (phase 3), and any started in the moment before the reload, into it.
    A rename on the same disk: one still being written carries on."""
    target, moved = MAIN_NAMES.rec_dir, 0
    if not os.path.isdir(target):
        return 0
    try:
        names = os.listdir(REC_DIR)
    except OSError:
        return 0
    for name in names:
        if REC_NAME_RE.fullmatch(name) and os.path.isfile(os.path.join(REC_DIR, name)):
            try:
                os.replace(os.path.join(REC_DIR, name), os.path.join(target, name))
                moved += 1
            except OSError as e:
                changes_log.info(f"Couldn't move recording {name} into {target}: {e}")
    if moved:
        changes_log.info(f"Moved {moved} recording(s) of the main tenant into {target}")
    return moved


def _tenant_folders(conn) -> List[str]:
    """Every tenant's recording folder, made by the dashboard (not by
    Asterisk on the first call) so both can use them: 2770, group
    asterisk, inside the folder the README sets up; and its greetings
    folder. The main tenant's greetings and notice from before it had a
    folder are copied into it; the old files are returned, to remove once
    Asterisk has the new paths."""
    old = []
    for t in all_tenant_names(conn):
        try:
            os.makedirs(t.ivr_dir, exist_ok=True)
        except OSError as e:
            changes_log.info(f"Couldn't make the greetings folder for tenant {t.code}: {e}")
        if t.id == MAIN_TENANT:
            for name in (os.listdir(IVR_DIR) if os.path.isdir(IVR_DIR) else []):
                src, dst = os.path.join(IVR_DIR, name), os.path.join(t.ivr_dir, name)
                if GREETING_FILE_RE.fullmatch(name) and os.path.isfile(src):
                    try:
                        if not os.path.exists(dst):
                            shutil.copy2(src, dst)
                        old.append(src)
                    except OSError as e:
                        changes_log.info(f"Couldn't copy {src} into {t.ivr_dir}: {e}")
        if os.path.isdir(t.rec_dir):
            continue
        # Made as 770, not chmod-ed to it afterwards: the service runs with
        # RestrictSUIDSGID, which refuses any chmod carrying the setgid bit.
        # The main folder's setgid passes it (and the asterisk group) on.
        old_mask = os.umask(0o007)
        try:
            os.makedirs(t.rec_dir, mode=0o770, exist_ok=True)
            try:
                os.chown(t.rec_dir, -1, grp.getgrnam("asterisk").gr_gid)
            except (KeyError, PermissionError):
                pass  # local dev: no asterisk group; the setgid parent sets it on the VPS
        except OSError as e:
            changes_log.info(f"Couldn't make the recordings folder for tenant {t.code}: {e}")
        finally:
            os.umask(old_mask)
    move_main_recordings()
    return old


def apply_config(conn, reason: str) -> dict:
    """Regenerate the config and apply it to this server's Asterisk."""
    old_greetings = _tenant_folders(conn)
    ok, messages = apply_to_node("", generate_config(conn))
    if ok:
        for path in old_greetings:  # Asterisk plays the copies in t<code> now
            try:
                os.remove(path)
            except OSError:
                pass
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
                         "application": f[5], "caller_id": f[7], "duration_seconds": duration,
                         "bridge": f[12] if len(f) > 13 else "", "uniqueid": f[13] if len(f) > 13 else ""})
    return channels


def recording_ids(folder: str = REC_DIR) -> set:
    """Uniqueids with a recording file: a call being recorded has one."""
    try:
        return {m.group(5) for m in map(REC_NAME_RE.fullmatch, os.listdir(folder)) if m}
    except OSError:
        return set()


def _mark_recording(channels: List[dict], t: TenantNames = MAIN_NAMES) -> List[dict]:
    """recording: this channel is being recorded (in tenant t's folder).
    recordable: one of t's extensions in a call, which the Record button
    can record."""
    ids = recording_ids(t.rec_dir) if channels else set()
    # A recording is made on one of the call's channels (the one that
    # answered, or the extension's for the button): mark the whole call.
    bridges = {c["bridge"] for c in channels if c["bridge"] and c["uniqueid"] in ids}
    for c in channels:
        c["recording"] = bool(c["uniqueid"]) and (c["uniqueid"] in ids or c["bridge"] in bridges)
        c["recordable"] = channel_ext(c["channel"], t.code) is not None and c["state"] == "Up"
    return channels


def get_status(t: TenantNames = MAIN_NAMES) -> dict:
    """Asterisk's state, with tenant t's phones and t's calls marked as
    recording or recordable. (Every tenant's calls are listed: phase 2,
    step 2.10.)"""
    version = asterisk_cli("core show version", timeout=4)
    if version is None:
        return {"asterisk_running": False, "checked_at": _now(), "extensions": {}, "trunks": {},
                "channels": [], "last_apply": _last_apply}
    contacts = parse_contacts(asterisk_cli("pjsip show contacts") or "")
    return {
        "asterisk_running": True,
        "version": version.strip().splitlines()[0] if version.strip() else "",
        "checked_at": _now(),
        "extensions": {endpoint_ext(k, t.code): v for k, v in contacts.items() if endpoint_ext(k, t.code)},
        "trunks": {k: contacts.get(k, {"status": "Unknown", "rtt_ms": None, "address": None}) for k in TRUNKS},
        "channels": _mark_recording(parse_channels(asterisk_cli("core show channels concise") or ""), t),
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
    return check_target(conn, target)


def _used_as_target(conn, number: str) -> Optional[str]:
    """What of the current tenant's still sends calls to number, if anything."""
    tid = current_tenant()
    if tid == MAIN_TENANT and get_settings(conn)["inbound_default"] == number:  # unlisted numbers: the main tenant's
        return "the default destination for inbound calls"
    r = conn.execute("SELECT did FROM inbound_routes WHERE target = ? AND tenant_id = ?", (number, tid)).fetchone()
    if r:
        return f"the destination for inbound number {r['did']}"
    r = conn.execute("SELECT label FROM call_links WHERE target = ? AND tenant_id = ?", (number, tid)).fetchone()
    if r:
        return f"the destination of call link \"{r['label']}\""
    for m in conn.execute("SELECT * FROM ivr_menus WHERE tenant_id = ? ORDER BY number", (tid,)):
        if m["number"] == number:
            continue  # a menu pointing at itself ("9 to hear this again") doesn't hold it
        uses = {m["fallback"], *menu_options(m).values()}
        if number in uses or f"vm:{number}" in uses:
            return f"used in IVR menu {m['number']} ({m['name']})"
    for h in conn.execute("SELECT * FROM business_hours WHERE tenant_id = ? ORDER BY number", (tid,)):
        if number in (h["open_target"], h["closed_target"]) or f"vm:{number}" in (h["open_target"], h["closed_target"]):
            return f"used in business hours {h['number']} ({h['name']})"
    return None


# ---------------------------------------------------------------------------
# Routes — auth is applied by main.py (include_router dependencies)
# ---------------------------------------------------------------------------

@router.get("/status")
def api_status():
    with get_db() as conn:
        t = tenant_names(conn, current_tenant())
    return get_status(t)


# ---------------------------------------------------------------------------
# Tenants (multi-tenant phase 2). Created and run from this dashboard; the
# switcher's cookie picks which one every Phone System page works on
# (main.py, _as_tenant).
# ---------------------------------------------------------------------------

TENANT_CODE_FIRST = 1002  # 1001 is the main tenant
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _tenant_json(r) -> dict:
    return {"id": r["id"], "code": r["code"], "name": r["name"], "status": r["status"], "sip_host": r["sip_host"],
            "main": r["id"] == MAIN_TENANT}


class TenantCreate(BaseModel):
    name: str
    outbound_cid: str  # required: what its calls show (and 112/999 without an extension's own)
    max_calls: int = 4  # calls in and out together
    sip_host: str = ""
    web_max: int = 10  # browser calls at once (call links): protection, not its plan


class TenantUpdate(BaseModel):
    name: str
    status: str = "active"  # or suspended
    sip_host: str = ""
    web_max: Optional[int] = None  # None = unchanged


def _web_max(value: int) -> int:
    if not 1 <= value <= WEB_MAX_LIMIT:
        raise HTTPException(400, f"Browser calls at once must be 1-{WEB_MAX_LIMIT}.")
    return value


def _host_free(conn, host: str, tenant_id: int = 0):
    """A hostname is one tenant's: moving a tenant by DNS mustn't move another."""
    if host and conn.execute("SELECT 1 FROM tenants WHERE sip_host = ? AND id != ?", (host, tenant_id)).fetchone():
        raise HTTPException(409, f"{host} is already another tenant's.")


def _clean_host(value: str) -> str:
    host = (value or "").strip().lower()
    if host and not HOST_RE.match(host):
        raise HTTPException(400, "The SIP hostname must look like acme.sip.example.ie.")
    if host and TENANT_HOST_SUFFIX and not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?" + re.escape(TENANT_HOST_SUFFIX), host):
        raise HTTPException(400, f"The SIP hostname must be one name ending {TENANT_HOST_SUFFIX}, "
                                 f"e.g. acme{TENANT_HOST_SUFFIX}: that's what the certificate covers.")
    return host


@router.get("/tenants")
def api_tenants():
    with get_db() as conn:
        counts = {r["tenant_id"]: r["n"] for r in conn.execute(
            "SELECT tenant_id, COUNT(*) AS n FROM extensions GROUP BY tenant_id")}
        return {"host_suffix": TENANT_HOST_SUFFIX, "tenants": [{**_tenant_json(r), "extensions": counts.get(r["id"], 0),
                             "settings": {k: get_settings(conn, r["id"])[k] for k in ("outbound_cid", "max_outbound", "web_max")}}
                            for r in conn.execute("SELECT * FROM tenants ORDER BY id")]}


@router.post("/tenants")
def api_create_tenant(req: TenantCreate):
    """A new tenant: the next code (never one used before, even by a tenant
    since removed: its AstDB keys, recordings and call records carry it),
    with its caller ID and call limit."""
    name = _clean_name(req.name)
    cid = req.outbound_cid.strip().replace(" ", "")
    if not CID_RE.match(cid):
        raise HTTPException(400, "Give the tenant's caller ID, e.g. +35312345678: its calls show it.")
    if not 1 <= req.max_calls <= 30:
        raise HTTPException(400, "Calls at once (in and out) must be 1-30.")
    host = _clean_host(req.sip_host)
    web_max = _web_max(req.web_max)
    with _lock, get_db() as conn:
        _host_free(conn, host)
        used = max([int(r["code"]) for r in conn.execute("SELECT code FROM tenants")] + [TENANT_CODE_FIRST - 1])
        code = max(int(get_settings(conn).get("tenant_code_next") or TENANT_CODE_FIRST), used + 1)
        if code > 9999:
            raise HTTPException(409, "No tenant codes left.")
        tid = conn.execute("INSERT INTO tenants (code, name, sip_host, created_at) VALUES (?, ?, ?, ?)",
                           (str(code), name, host, _now())).lastrowid
        _set_setting(conn, "tenant_code_next", str(code + 1))
        _set_setting(conn, "outbound_cid", cid, tenant=tid)
        _set_setting(conn, "max_outbound", str(req.max_calls), tenant=tid)
        _set_setting(conn, "web_max", str(web_max), tenant=tid)
        conn.commit()
        result = apply_config(conn, f"Created tenant {code} ({name}): caller ID {cid}, {req.max_calls} calls at once")
    return {"id": tid, "code": str(code), **result}


@router.put("/tenants/{tenant_id}")
def api_update_tenant(tenant_id: int, req: TenantUpdate):
    name = _clean_name(req.name)
    if req.status not in ("active", "suspended"):
        raise HTTPException(400, "A tenant is active or suspended.")
    if tenant_id == MAIN_TENANT and req.status != "active":
        raise HTTPException(400, "The main tenant can't be suspended.")
    host = _clean_host(req.sip_host)
    web_max = None if req.web_max is None else _web_max(req.web_max)
    with _lock, get_db() as conn:
        _host_free(conn, host, tenant_id)
        if conn.execute("UPDATE tenants SET name = ?, status = ?, sip_host = ? WHERE id = ?",
                        (name, req.status, host, tenant_id)).rowcount == 0:
            raise HTTPException(404, "Tenant not found.")
        if web_max is not None:
            _set_setting(conn, "web_max", str(web_max), tenant=tenant_id)
        conn.commit()
        return apply_config(conn, f"Tenant {tenant_id}: {name}, {req.status}, host {host or 'none'}"
                                  + (f", {web_max} browser calls at once" if web_max is not None else ""))


@router.get("/config")
def api_config():
    tid = current_tenant()
    with get_db() as conn:
        s = get_settings(conn, tid)
        t = tenant_names(conn, tid)
        return {
            "extensions": [{"ext": r["ext"], "name": r["name"], "enabled": bool(r["enabled"]),
                            "voicemail_email": r["vm_email"], "setup_email": r["setup_email"],
                            "outbound_cid": r["outbound_cid"], "record": bool(r["record"]),
                            "vm_nopin": bool(r["vm_nopin"]),
                            "created_at": r["created_at"], "updated_at": r["updated_at"]}
                           for r in conn.execute("SELECT * FROM extensions WHERE tenant_id = ? ORDER BY ext", (tid,))],
            "ring_groups": [{"number": r["number"], "name": r["name"], "strategy": r["strategy"],
                             "ring_time": r["ring_time"], "members": _members(r), "voicemail": r["voicemail"]}
                            for r in conn.execute("SELECT * FROM ring_groups WHERE tenant_id = ? ORDER BY number", (tid,))],
            "inbound_routes": [dict(r) for r in conn.execute("SELECT * FROM inbound_routes WHERE tenant_id = ? ORDER BY did",
                                                             (tid,))],
            "ivr_menus": [{"number": r["number"], "name": r["name"], "options": menu_options(r),
                           "fallback": r["fallback"], "timeout": r["timeout"], "retries": r["retries"],
                           "direct_dial": bool(r["direct_dial"]), "greeting": os.path.exists(greeting_path(r["number"], t))}
                          for r in conn.execute("SELECT * FROM ivr_menus WHERE tenant_id = ? ORDER BY number", (tid,))],
            "business_hours": [{"number": r["number"], "name": r["name"], "week": hours_week(r),
                                "holidays": hours_holidays(r), "open_target": r["open_target"],
                                "closed_target": r["closed_target"], "override": r["override"],
                                "open_now": hours_open_now(r)}
                               for r in conn.execute("SELECT * FROM business_hours WHERE tenant_id = ? ORDER BY number",
                                                     (tid,))],
            "tenant": _tenant_json(conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone()),
            "tenants": [_tenant_json(r) for r in conn.execute("SELECT * FROM tenants ORDER BY id")],
            "timezone": PBX_TIMEZONE,
            "settings": {
                "outbound_cid": s["outbound_cid"],
                "max_outbound": int(s["max_outbound"]),
                "main_mailbox": s["main_mailbox"],
                "voxbeam_prefix": s["voxbeam_prefix"],
                "trunk_order": [t for t in s["trunk_order"].split(",") if t in TRUNKS],
                "trunks_enabled": {t: s.get(f"trunk_{t}_enabled") == "1" for t in TRUNKS},
                "inbound_default": s["inbound_default"],
                "routes": {k: s.get(f"route_{k}", "default") for k in NUMBER_TYPES},
                "caps": {"default": s.get("cap_default", ""), **{k: s.get(f"cap_{k}", "") for k in NUMBER_TYPES}},
                "lcr_allow": {f"{t}{r}": s.get(f"lcr_allow_{t}{r}", "1") == "1" for t, r in ALL_ROUTES},
                "cli_countries": {f"{t}{r}": s.get(f"cli_{t}{r}", "") for t, r in ALL_ROUTES},
                "web_max_total": int(s["web_max_total"]),
                "video": s["video"] == "1",
                "trunk_max": {t: s.get(f"trunk_max_{t}", "") for t in TRUNKS},
            },
            "all_routes": [{"key": f"{t}{r}", "label": TRUNKS[t]["label"] + (f" {VOXBEAM_ROUTES[r]}" if r else "")}
                           for t, r in ALL_ROUTES],
            "trunks": {k: {"label": v["label"], "host": v["host"]} for k, v in TRUNKS.items()},
            "number_types": NUMBER_TYPES,
            "voxbeam_routes": VOXBEAM_ROUTES,
            "limits": {"ext_min": EXT_MIN, "ext_max": EXT_MAX, "group_min": GROUP_MIN, "group_max": GROUP_MAX,
                       "menu_min": MENU_MIN, "menu_max": MENU_MAX, "hours_min": HOURS_MIN, "hours_max": HOURS_MAX},
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
    tid = current_tenant()
    with _lock, get_db() as conn:
        taken = {r["ext"] for r in conn.execute("SELECT ext FROM extensions WHERE tenant_id = ?", (tid,))}
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
        conn.execute("INSERT INTO extensions (ext, name, password, vm_pin, enabled, created_at, updated_at, tenant_id) "
                     "VALUES (?, ?, ?, ?, 1, ?, ?, ?)", (ext, name, secrets.token_urlsafe(24), _new_pin(), now, now, tid))
        conn.commit()
        result = apply_config(conn, f"Created extension {ext} ({name})")
    return {"ext": ext, **result}


@router.put("/extensions/{ext}")
def api_update_extension(ext: str, req: ExtensionUpdate):
    name = _clean_name(req.name)
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET name = ?, enabled = ?, updated_at = ? WHERE ext = ? AND tenant_id = ?",
                        (name, int(req.enabled), _now(), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        if not req.enabled:
            _revoke_setup_links(conn, ext)
        conn.commit()
        return apply_config(conn, f"Updated extension {ext}: name={name}, enabled={req.enabled}")


@router.post("/extensions/{ext}/reset-password")
def api_reset_password(ext: str):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET password = ?, updated_at = ? WHERE ext = ? AND tenant_id = ?",
                        (secrets.token_urlsafe(24), _now(), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        _revoke_setup_links(conn, ext)
        conn.commit()
        return apply_config(conn, f"Reset password for extension {ext}")


class VoicemailEmailRequest(BaseModel):
    email: str = ""  # empty = don't email this mailbox's messages


@router.put("/extensions/{ext}/voicemail-email")
def api_voicemail_email(ext: str, req: VoicemailEmailRequest):
    address = req.email.strip()
    # , ; | = would break the mailbox's line in voicemail_portal.conf
    if address and (not EMAIL_RE.match(address) or re.search(r"[,;|=]", address)):
        raise HTTPException(400, "That doesn't look like an email address.")
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET vm_email = ?, updated_at = ? WHERE ext = ? AND tenant_id = ?",
                        (address, _now(), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"Voicemail for extension {ext} emailed to {address or 'nobody'}")


class OutboundCidRequest(BaseModel):
    outbound_cid: str = ""  # empty = use the default caller ID


@router.put("/extensions/{ext}/outbound-cid")
def api_extension_outbound_cid(ext: str, req: OutboundCidRequest):
    cid = req.outbound_cid.strip().replace(" ", "")
    if cid and not CID_RE.match(cid):
        raise HTTPException(400, "Caller ID must be a phone number, e.g. +35312345678.")
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET outbound_cid = ?, updated_at = ? WHERE ext = ? AND tenant_id = ?",
                        (cid, _now(), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"Outbound caller ID for extension {ext}: {cid or 'the default'}")


_DB_LINE = re.compile(r"^/(CF|CFB|CFU|DND)/(\d+)\s*:\s*(\S*)\s*$")


def read_forwarding(t: TenantNames = MAIN_NAMES) -> Optional[Dict[str, dict]]:
    """{ext: {"dnd": bool, "always": "...", "busy": "...", "no_answer": "..."}}
    for tenant t's extensions, from AstDB (keyed by login), or None if
    Asterisk can't be asked."""
    out = asterisk_cli("database show")
    if out is None:
        return None
    names = {v: k for k, v in FORWARD_FAMILIES.items()}
    found: Dict[str, dict] = {}
    for line in out.splitlines():
        m = _DB_LINE.match(line.strip())
        if not m:
            continue
        family, login, value = m.groups()
        p = parse_login(login)
        if not p or p[0] != t.code:
            continue  # another tenant's
        ext = p[1]
        entry = found.setdefault(ext, {"dnd": False, "always": "", "busy": "", "no_answer": ""})
        if family == DND_FAMILY:
            entry["dnd"] = value == "1"
        else:
            entry[names[family]] = value
    return found


def _db_set(family: str, login: str, value: str):
    cmd = f"database put {family} {login} {value}" if value else f"database del {family} {login}"
    out = asterisk_cli(cmd)
    if out is None:
        raise HTTPException(503, "Asterisk isn't answering, so nothing was changed. Is it running?")
    if value and "updated" not in out.lower():
        raise HTTPException(502, f"Asterisk didn't save it: {out.strip()[:200]}")


def forward_target(conn, ext: str, value: str) -> str:
    """The number as stored and dialled, or "" for off. Only other extensions
    and Irish landlines and mobiles (see FORWARD_TARGET_DIALPLAN_RE)."""
    v = re.sub(r"[\s()-]", "", value or "")
    if not v:
        return ""
    if re.fullmatch(r"2\d\d", v):
        if v == ext:
            raise HTTPException(400, "An extension can't forward to itself.")
        if not conn.execute("SELECT 1 FROM extensions WHERE ext = ? AND tenant_id = ?", (v, current_tenant())).fetchone():
            raise HTTPException(400, f"There's no extension {v}.")
        return v
    kind, national, _ = classify_dialled(v)
    if kind not in ("mobile", "landline") or not re.fullmatch(r"0[1-9]\d{7,8}", national):
        raise HTTPException(400, f"{value.strip()} can't be used: forward only to an extension or an Irish landline "
                                 "or mobile (not abroad, 1800 or premium numbers).")
    return national


@router.get("/tenant")
def api_own_tenant():
    """The tenant being worked on: its code, name and status, nothing else
    (for the tenant portal, which never sees the tenants list)."""
    with get_db() as conn:
        r = conn.execute("SELECT code, name, status FROM tenants WHERE id = ?", (current_tenant(),)).fetchone()
    return {"code": r["code"], "name": r["name"], "status": r["status"]}


@router.get("/extensions")
def api_list_extensions():
    """The tenant's extensions, without passwords or PINs: for the tenant
    portal, which gets nothing else of /config. created_at tells one
    extension from a later one with the same number."""
    with get_db() as conn:
        rows = conn.execute("SELECT ext, name, enabled, created_at, vm_email, record, vm_nopin FROM extensions "
                            "WHERE tenant_id = ? ORDER BY ext", (current_tenant(),)).fetchall()
    return {"extensions": [{"ext": r["ext"], "name": r["name"], "enabled": bool(r["enabled"]),
                            "created_at": r["created_at"], "vm_email": r["vm_email"], "record": bool(r["record"]),
                            "vm_nopin": bool(r["vm_nopin"])} for r in rows]}


@router.get("/status/tenant")
def api_tenant_status():
    """The tenant's own phones and their calls only (the full Status lists
    every tenant's calls and the trunks): for the tenant portal."""
    with get_db() as conn:
        t = tenant_names(conn, current_tenant())
    s = get_status(t)
    calls = [{"ext": channel_ext(c["channel"], t.code), "state": c["state"], "caller_id": c["caller_id"],
              "duration_seconds": c["duration_seconds"], "recording": c.get("recording", False)}
             for c in s["channels"] if channel_ext(c["channel"], t.code)]
    return {"asterisk_running": s["asterisk_running"], "checked_at": s["checked_at"],
            "phones": {ext: {"status": v.get("status")} for ext, v in s["extensions"].items()}, "calls": calls}


@router.get("/routing")
def api_routing():
    """The tenant's ring groups, IVR menus and business hours, the places a
    call can be sent (built here, with labels, so a caller never has to
    know how they're written), and its numbers with where they go (read
    only). For the tenant portal, which gets nothing else of /config."""
    tid = current_tenant()
    with get_db() as conn:
        t = tenant_names(conn, tid)
        exts = conn.execute("SELECT ext, name, enabled FROM extensions WHERE tenant_id = ? ORDER BY ext", (tid,)).fetchall()
        groups = conn.execute("SELECT * FROM ring_groups WHERE tenant_id = ? ORDER BY number", (tid,)).fetchall()
        menus = conn.execute("SELECT * FROM ivr_menus WHERE tenant_id = ? ORDER BY number", (tid,)).fetchall()
        hours = conn.execute("SELECT * FROM business_hours WHERE tenant_id = ? ORDER BY number", (tid,)).fetchall()
        numbers = conn.execute("SELECT did, label, target FROM inbound_routes WHERE tenant_id = ? ORDER BY did",
                               (tid,)).fetchall()
    places = ([{"value": e["ext"], "label": f"{e['ext']} · {e['name']}", "kind": "extension",
                "enabled": bool(e["enabled"])} for e in exts]
              + [{"value": g["number"], "label": f"{g['number']} · {g['name']}", "kind": "group", "enabled": True}
                 for g in groups]
              + [{"value": m["number"], "label": f"{m['number']} · {m['name']}", "kind": "menu", "enabled": True}
                 for m in menus]
              + [{"value": h["number"], "label": f"{h['number']} · {h['name']}", "kind": "hours", "enabled": True}
                 for h in hours]
              + [{"value": f"vm:{e['ext']}", "label": f"Voicemail {e['ext']} · {e['name']}", "kind": "voicemail",
                  "enabled": bool(e["enabled"])} for e in exts]
              + [{"value": "hangup", "label": "Hang up", "kind": "hangup", "enabled": True}])
    return {
        "ring_groups": [{"number": r["number"], "name": r["name"], "strategy": r["strategy"],
                         "ring_time": r["ring_time"], "members": _members(r), "voicemail": r["voicemail"]} for r in groups],
        "ivr_menus": [{"number": r["number"], "name": r["name"], "options": menu_options(r), "fallback": r["fallback"],
                       "timeout": r["timeout"], "retries": r["retries"], "direct_dial": bool(r["direct_dial"]),
                       "greeting": os.path.exists(greeting_path(r["number"], t))} for r in menus],
        "business_hours": [{"number": r["number"], "name": r["name"], "week": hours_week(r), "holidays": hours_holidays(r),
                            "open_target": r["open_target"], "closed_target": r["closed_target"],
                            "override": r["override"], "open_now": hours_open_now(r)} for r in hours],
        "destinations": places,
        "numbers": [{"did": r["did"], "label": r["label"], "target": r["target"]} for r in numbers],
        "limits": {"group_min": GROUP_MIN, "group_max": GROUP_MAX, "menu_min": MENU_MIN, "menu_max": MENU_MAX,
                   "hours_min": HOURS_MIN, "hours_max": HOURS_MAX},
        "timezone": PBX_TIMEZONE,
    }


@router.get("/forwarding")
def api_forwarding():
    with get_db() as conn:
        t = tenant_names(conn, current_tenant())
    data = read_forwarding(t)
    return {"available": data is not None, "extensions": data or {}}


class ForwardingRequest(BaseModel):
    dnd: bool = False
    always: str = ""
    busy: str = ""
    no_answer: str = ""


@router.put("/extensions/{ext}/forwarding")
def api_set_forwarding(ext: str, req: ForwardingRequest):
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM extensions WHERE ext = ? AND tenant_id = ?", (ext, current_tenant())).fetchone():
            raise HTTPException(404, "Extension not found.")
        targets = {k: forward_target(conn, ext, getattr(req, k)) for k in FORWARD_FAMILIES}
        login = tenant_names(conn, current_tenant()).login(ext)
    with _lock:
        _db_set(DND_FAMILY, login, "1" if req.dnd else "")
        for key, family in FORWARD_FAMILIES.items():
            _db_set(family, login, targets[key])
    summary = ", ".join([f"{k}={v}" for k, v in targets.items() if v] + (["dnd"] if req.dnd else [])) or "all off"
    changes_log.info(f"Forwarding for extension {ext}: {summary}")
    return {"ok": True, **targets, "dnd": req.dnd}


def _clear_forwarding(t: TenantNames, ext: str):
    """An extension's own settings, and its tenant's forwards to it, when
    it's deleted."""
    data = read_forwarding(t) or {}
    for family in [DND_FAMILY, *FORWARD_FAMILIES.values()]:
        asterisk_cli(f"database del {family} {t.login(ext)}")
    for other, entry in data.items():
        for key, family in FORWARD_FAMILIES.items():
            if entry.get(key) == ext:
                asterisk_cli(f"database del {family} {t.login(other)}")


class ClickToCallRequest(BaseModel):
    ext: str
    number: str


def click_to_call_target(conn, ext: str, number: str) -> str:
    """The number as the dialplan will dial it (digits only), or 400. Never
    emergency numbers: a tel: link from any web page can fill this in."""
    v = re.sub(r"[\s().-]", "", number or "")
    if re.fullmatch(r"2\d\d", v):
        if v == ext:
            raise HTTPException(400, "That's the extension you're calling from.")
        if not conn.execute("SELECT 1 FROM extensions WHERE ext = ? AND enabled = 1 AND tenant_id = ?",
                            (v, current_tenant())).fetchone():
            raise HTTPException(400, f"There's no extension {v}.")
        return v
    kind, national, _ = classify_dialled(v)
    if kind == "emergency":
        raise HTTPException(400, "Dial emergency numbers from the phone itself.")
    if not kind or not re.fullmatch(r"\d{6,20}", national):
        raise HTTPException(400, f"{number.strip()} isn't a number the phones can call.")
    return national


@router.post("/click-to-call")
def api_click_to_call(req: ClickToCallRequest):
    with get_db() as conn:
        e = conn.execute("SELECT * FROM extensions WHERE ext = ? AND tenant_id = ?", (req.ext, current_tenant())).fetchone()
        if not e or not e["enabled"]:
            raise HTTPException(400, "Choose an extension that's switched on.")
        target = click_to_call_target(conn, e["ext"], req.number)
        t = tenant_names(conn, current_tenant())
    # Only digits reach the command line (checked above).
    leg = f"{e['ext']}{target}"
    out = asterisk_cli(f"channel originate Local/{leg}@{t.c2c_ring}/n extension {leg}@{t.c2c_dial}")
    changes_log.info(f"Click to call: extension {e['ext']} to {target}")
    if out is None:
        raise HTTPException(502, "Asterisk didn't answer, so the call may not have started. Check your phone.")
    if re.search(r"no such command|usage:|unable to", out, re.I):
        raise HTTPException(502, f"Asterisk said: {out.strip()[:200]}")
    return {"ok": True, "number": target, "asterisk_said": out.strip()[:200]}


WEAK_PINS = {"1234", "12345", "123456", "1234567", "12345678", "4321", "654321", "1111", "0000"}


class VoicemailPinRequest(BaseModel):
    pin: str


def _clean_pin(pin: str, ext: str) -> str:
    """A voicemail PIN its user chose: 4-8 digits, not one anyone would
    guess first (all the same digit, a run up or down, the extension)."""
    pin = (pin or "").strip()
    if not re.fullmatch(r"[0-9]{4,8}", pin):
        raise HTTPException(400, "A PIN is 4 to 8 digits.")
    run = "01234567890"
    if (len(set(pin)) == 1 or pin in WEAK_PINS or pin in run or pin in run[::-1] or ext in pin):
        raise HTTPException(400, "That PIN is too easy to guess: avoid repeats, runs like 1234, and the extension number.")
    return pin


@router.put("/extensions/{ext}/voicemail-pin")
def api_set_voicemail_pin(ext: str, req: VoicemailPinRequest):
    pin = _clean_pin(req.pin, ext)
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET vm_pin = ?, updated_at = ? WHERE ext = ? AND tenant_id = ?",
                        (pin, _now(), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"Voicemail PIN for extension {ext} chosen")  # never the PIN itself


class VoicemailNoPinRequest(BaseModel):
    on: bool


@router.put("/extensions/{ext}/voicemail-nopin")
def api_voicemail_nopin(ext: str, req: VoicemailNoPinRequest):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET vm_nopin = ?, updated_at = ? WHERE ext = ? AND tenant_id = ?",
                        (int(req.on), _now(), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"Voicemail for extension {ext} from its own phones: "
                                  + ("no PIN" if req.on else "PIN asked"))


@router.post("/extensions/{ext}/reset-voicemail-pin")
def api_reset_voicemail_pin(ext: str):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET vm_pin = ?, updated_at = ? WHERE ext = ? AND tenant_id = ?",
                        (_new_pin(), _now(), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"New voicemail PIN for extension {ext}")


@router.get("/extensions/{ext}/credentials")
def api_credentials(ext: str):
    with get_db() as conn:
        r = conn.execute("SELECT ext, password, vm_pin FROM extensions WHERE ext = ? AND tenant_id = ?",
                         (ext, current_tenant())).fetchone()
    if not r:
        raise HTTPException(404, "Extension not found.")
    changes_log.info(f"Viewed credentials for extension {ext}")
    return {"username": row_login({"ext": r["ext"], "tenant_id": current_tenant()}), "password": r["password"],
            "server": sip_host_for(current_tenant()),
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
    conn.execute("DELETE FROM provision_tokens WHERE ext = ? AND tenant_id = ?", (ext, current_tenant()))


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
    tid = current_tenant()
    with _lock, get_db() as conn:
        e = conn.execute("SELECT * FROM extensions WHERE ext = ? AND enabled = 1 AND tenant_id = ?", (ext, tid)).fetchone()
        if not e:
            raise HTTPException(404, "Extension not found, or turned off.")
        conn.execute("DELETE FROM provision_tokens WHERE expires_at < ?", (now.isoformat(),))
        conn.execute("INSERT INTO provision_tokens (token_hash, ext, expires_at, tenant_id) VALUES (?, ?, ?, ?)",
                     (_token_hash(token), ext, (now + ttl).isoformat(), tid))
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
    # Linphone has no paste-a-link option, only Scan QR code, but it opens
    # linphone-config: links (tested with Linphone 6 on an iPhone).
    link = html.escape(f"linphone-config:{_setup_url(token, 'linphone')}")
    return _setup_page(f"Set up extension {html.escape(r['ext'])} in Linphone", f"""
<p>Linphone must be installed on this phone first.</p>
<p><a class="btn" href="{link}">Open in Linphone</a></p>
<p>If that doesn't work, open the setup email on a computer, then in Linphone tap <b>Scan QR code</b>
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
        # The token's own tenant: a phone fetching its setup has no dashboard tenant.
        e = r and conn.execute("SELECT * FROM extensions WHERE ext = ? AND enabled = 1 AND tenant_id = ?",
                               (r["ext"], r["tenant_id"])).fetchone()
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
    """The account settings, in Acrobits' Account XML format, only for
    Acrobits' apps (Groundwire says e.g. "Groundwire/26.1.18 (build ...;
    iOS ...)"; Acrobits Softphone and Cloud Softphone say Acrobits). A
    browser gets the Open in Groundwire page; anything else, such as an
    email security scanner, gets nothing. Neither uses the token up."""
    agent = request.headers.get("user-agent", "").lower()
    if "groundwire" not in agent and "acrobits" not in agent:
        if "mozilla" in agent:
            return prov_page(token)
        with get_db() as conn:
            live = _live_token(conn, token) is not None
        return Response("Open this link from Groundwire." if live else _GONE, status_code=403 if live else 404,
                        media_type="text/plain", headers=_NO_STORE)
    e = _redeem(token, request, "Groundwire")
    if not e:
        return _gone()
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<account>
  <title>{x(e["name"])} ({x(e["ext"])})</title>
  <username>{x(row_login(e))}</username>
  <password>{x(e["password"])}</password>
  <userDisplayName>{x(e["name"])}</userDisplayName>
  <host>{x(sip_host_for(e["tenant_id"]))}:{x(PUBLIC_PORT)}</host>
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
    tid = current_tenant()
    with _lock, get_db() as conn:
        if not conn.execute("SELECT 1 FROM extensions WHERE ext = ? AND tenant_id = ?", (ext, tid)).fetchone():
            raise HTTPException(404, "Extension not found.")
        used = _used_as_target(conn, ext)
        if used:
            raise HTTPException(409, f"Extension {ext} is {used}. Change that first.")
        for g in conn.execute("SELECT * FROM ring_groups WHERE tenant_id = ?", (tid,)).fetchall():
            if ext in _members(g):
                conn.execute("UPDATE ring_groups SET members = ? WHERE number = ? AND tenant_id = ?",
                             (",".join(m for m in _members(g) if m != ext), g["number"], tid))
        conn.execute("DELETE FROM extensions WHERE ext = ? AND tenant_id = ?", (ext, tid))
        conn.execute("UPDATE ring_groups SET voicemail = '' WHERE voicemail = ? AND tenant_id = ?", (ext, tid))
        _revoke_setup_links(conn, ext)
        conn.commit()
        _clear_forwarding(tenant_names(conn, tid), ext)
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
    exts = {r["ext"] for r in conn.execute("SELECT ext FROM extensions WHERE tenant_id = ?", (current_tenant(),))}
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
    tid = current_tenant()
    with _lock, get_db() as conn:
        g = _clean_group(conn, req)
        taken = {r["number"] for r in conn.execute("SELECT number FROM ring_groups WHERE tenant_id = ?", (tid,))}
        if req.number:
            number = _in_range(req.number, GROUP_MIN, GROUP_MAX, "Ring group number")
            if number in taken:
                raise HTTPException(409, f"Ring group {number} already exists.")
        else:
            free = [str(n) for n in range(GROUP_MIN, GROUP_MAX + 1) if str(n) not in taken]
            if not free:
                raise HTTPException(409, "No free ring group numbers left.")
            number = free[0]
        conn.execute("INSERT INTO ring_groups (number, name, strategy, ring_time, members, voicemail, tenant_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (number, g["name"], g["strategy"], g["ring_time"], g["members"], g["voicemail"], tid))
        conn.commit()
        result = apply_config(conn, f"Created ring group {number} ({g['name']}): {g['strategy']} {g['members']}, "
                                    f"voicemail={g['voicemail'] or 'none'}")
    return {"number": number, **result}


@router.put("/ringgroups/{number}")
def api_update_group(number: str, req: RingGroupRequest):
    with _lock, get_db() as conn:
        g = _clean_group(conn, req)
        if conn.execute("UPDATE ring_groups SET name = ?, strategy = ?, ring_time = ?, members = ?, voicemail = ? "
                        "WHERE number = ? AND tenant_id = ?",
                        (g["name"], g["strategy"], g["ring_time"], g["members"], g["voicemail"], number,
                         current_tenant())).rowcount == 0:
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
        if conn.execute("DELETE FROM ring_groups WHERE number = ? AND tenant_id = ?", (number, current_tenant())).rowcount == 0:
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
    tid = current_tenant()
    with _lock, get_db() as conn:
        target = _valid_target(conn, req.target)
        # A number is one tenant's: saving it never takes it from another.
        owner = conn.execute("SELECT tenant_id FROM inbound_routes WHERE did = ?", (did,)).fetchone()
        if owner and owner["tenant_id"] != tid:
            raise HTTPException(409, f"{did} belongs to another tenant.")
        conn.execute("INSERT INTO inbound_routes (did, target, label, tenant_id) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(did) DO UPDATE SET target = excluded.target, label = excluded.label",
                     (did, target, label, tid))
        conn.commit()
        return {"did": did, **apply_config(conn, f"Inbound route {did} -> {target}")}


class InboundTargetRequest(BaseModel):
    target: str


@router.put("/inbound/{did}/target")
def api_inbound_target(did: str, req: InboundTargetRequest):
    """Where one of the current tenant's numbers sends calls, and nothing
    else: never adds, takes or removes a number (that stays the
    operator's), and anything not the tenant's is simply not found. For
    tenant admins in the portal."""
    with _lock, get_db() as conn:
        target = _valid_target(conn, req.target)
        if conn.execute("UPDATE inbound_routes SET target = ? WHERE did = ? AND tenant_id = ?",
                        (target, normalize_did(did), current_tenant())).rowcount == 0:
            raise HTTPException(404, "Number not found.")
        conn.commit()
        return apply_config(conn, f"Inbound route {normalize_did(did)} -> {target}")


@router.delete("/inbound/{did}")
def api_delete_inbound_route(did: str):
    with _lock, get_db() as conn:
        if conn.execute("DELETE FROM inbound_routes WHERE did = ? AND tenant_id = ?", (did, current_tenant())).rowcount == 0:
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
    caps: Optional[Dict[str, str]] = None  # "default" or a number type -> € per minute ("" = the default)
    lcr_allow: Optional[Dict[str, bool]] = None  # route key (irishvoip, voxbeam0011103...) -> usable for cheapest
    cli_countries: Optional[Dict[str, str]] = None  # route key -> calling codes it can show ("353,61"; "" = any)
    web_max_total: Optional[int] = None  # browser calls at once, all tenants; None = unchanged
    video: Optional[bool] = None  # None = unchanged
    main_mailbox: Optional[str] = None  # None = unchanged; "" = the lowest extension
    trunk_max: Optional[Dict[str, str]] = None  # trunk -> most calls at once ("" = no ceiling); None = unchanged


def _cap(value: str, what: str) -> str:
    v = (value or "").strip().replace("€", "").replace(",", ".")
    if not v:
        return ""
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise HTTPException(400, f"{what} must be an amount per minute, e.g. 0.05.")
    if not 0 <= d <= 10:
        raise HTTPException(400, f"{what} must be between €0 and €10 a minute.")
    return format(d.normalize(), "f")


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
    caps = {}
    for k, v in (req.caps or {}).items():
        if k != "default" and k not in NUMBER_TYPES:
            raise HTTPException(400, "Unknown number type for a price cap.")
        caps[k] = _cap(v, "The default price cap" if k == "default" else f"The {NUMBER_TYPES[k].split(' (')[0]} cap")
    if req.caps is not None and not caps.get("default"):
        raise HTTPException(400, "Set a default price cap (e.g. 0.05). It's what stops an expensive call.")
    if req.web_max_total is not None and not 1 <= req.web_max_total <= WEB_MAX_TOTAL_LIMIT:
        raise HTTPException(400, f"Browser calls at once, all tenants, must be 1-{WEB_MAX_TOTAL_LIMIT}.")
    cli_countries = {}
    for k, v in (req.cli_countries or {}).items():
        if k not in {f"{t}{r}" for t, r in ALL_ROUTES}:
            raise HTTPException(400, "Unknown route.")
        codes = [c.strip().lstrip("+") for c in (v or "").split(",") if c.strip()]
        if any(not re.fullmatch(r"[1-9][0-9]{0,3}", c) for c in codes):
            raise HTTPException(400, "Caller-ID countries are calling codes, e.g. 353 or 61,353.")
        cli_countries[k] = ",".join(dict.fromkeys(codes))
    allow = req.lcr_allow or {}
    known = {f"{t}{r}" for t, r in ALL_ROUTES}
    if set(allow) - known:
        raise HTTPException(400, "Unknown route.")
    ceilings = {}
    for t, v in (req.trunk_max or {}).items():
        if t not in TRUNKS:
            raise HTTPException(400, "Unknown trunk for a ceiling.")
        v = str(v).strip()
        if v and not (v.isdigit() and 1 <= int(v) <= TRUNK_MAX_LIMIT):
            raise HTTPException(400, f"{TRUNKS[t]['label']}'s most calls at once must be 1-{TRUNK_MAX_LIMIT}, "
                                     "or empty for no ceiling.")
        ceilings[t] = str(int(v)) if v else ""
    with _lock, get_db() as conn:
        # Numbers not listed anywhere are the main tenant's, whoever saves.
        default = check_target(conn, req.inbound_default, tenant=MAIN_TENANT)
        if req.main_mailbox is not None:
            box = req.main_mailbox.strip()
            if box and box not in destinations(conn)["exts"]:
                raise HTTPException(400, f"There's no extension {box} for the main mailbox.")
            _set_setting(conn, "main_mailbox", box)
        _set_setting(conn, "outbound_cid", cid)
        _set_setting(conn, "max_outbound", req.max_outbound)
        _set_setting(conn, "voxbeam_prefix", req.voxbeam_prefix)
        _set_setting(conn, "trunk_order", ",".join(req.trunk_order))
        for t in TRUNKS:
            _set_setting(conn, f"trunk_{t}_enabled", "1" if req.trunks_enabled.get(t) else "0")
        _set_setting(conn, "inbound_default", default)
        for kind, route in routes.items():
            _set_setting(conn, f"route_{kind}", route)
        for k, v in caps.items():
            _set_setting(conn, f"cap_{k}", v)
        for k, v in allow.items():
            _set_setting(conn, f"lcr_allow_{k}", "1" if v else "0")
        for k, v in cli_countries.items():
            _set_setting(conn, f"cli_{k}", v)
        if req.web_max_total is not None:
            _set_setting(conn, "web_max_total", str(req.web_max_total))
        if req.video is not None:
            _set_setting(conn, "video", "1" if req.video else "0")
        for t, v in ceilings.items():
            _set_setting(conn, f"trunk_max_{t}", v)
        conn.commit()
        enabled = [t for t in req.trunk_order if req.trunks_enabled.get(t)]
        return apply_config(conn, f"Settings: cid={cid}, max={req.max_outbound}, voxbeam={req.voxbeam_prefix}, "
                                  f"outbound order={enabled or 'NONE'}, inbound default={default}, "
                                  f"routes={routes or 'unchanged'}, caps={caps or 'unchanged'}, "
                                  f"cheapest-route use={allow or 'unchanged'}, "
                                  f"video={'unchanged' if req.video is None else req.video}, "
                                  f"carrier ceilings={ceilings or 'unchanged'}, "
                                  f"caller-ID countries={cli_countries or 'unchanged'}")


@router.post("/apply")
def api_apply():
    """Regenerate and reload without changing anything (e.g. after editing pjsip.conf by hand)."""
    with _lock, get_db() as conn:
        return apply_config(conn, "Manual re-apply")


@public_router.get("/{token}/linphone.xml")
def prov_linphone(token: str, request: Request):
    """The account settings as a Linphone remote-provisioning file, only
    for Linphone itself (it says e.g. "LinphoneiOS/6.2.3 (iPhone)
    LinphoneSDK/5.5.23"). A browser (someone scanning with the Camera app)
    gets the Open in Linphone page; anything else, such as an email
    security scanner following the QR code, gets nothing. Neither uses the
    token up or sees the password."""
    agent = request.headers.get("user-agent", "").lower()
    if "linphone" not in agent:
        with get_db() as conn:
            r = _live_token(conn, token)
        if not r:
            return HTMLResponse(f"<p>{_GONE}</p>", status_code=404, headers=_NO_STORE)
        if "mozilla" in agent:
            return _linphone_page(token, r)
        return Response("Open this link from Linphone.", status_code=403, media_type="text/plain", headers=_NO_STORE)
    e = _redeem(token, request, "Linphone")
    if not e:
        return _gone()
    host = sip_host_for(e["tenant_id"])
    server = f"&lt;sip:{x(host)}:{x(PUBLIC_PORT)};transport=tls&gt;"
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
    <entry name="reg_identity" overwrite="true">"{name}" &lt;sip:{x(row_login(e))}@{x(host)}&gt;</entry>
    <entry name="reg_expires" overwrite="true">600</entry>
    <entry name="reg_sendregister" overwrite="true">1</entry>
    <entry name="publish" overwrite="true">0</entry>
  </section>
  <section name="auth_info_0" overwrite="true">
    <entry name="username" overwrite="true">{x(row_login(e))}</entry>
    <entry name="userid" overwrite="true">{x(row_login(e))}</entry>
    <entry name="passwd" overwrite="true">{x(e["password"])}</entry>
    <entry name="domain" overwrite="true">{x(host)}</entry>
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


def _graph_ready() -> bool:
    return bool(MS_GRAPH_TENANT_ID and MS_GRAPH_CLIENT_ID and MS_GRAPH_CLIENT_SECRET and EMAIL_FROM_ADDRESS)


def _graph_send(to: str, subject: str, body_html: str, inline_images: Dict[str, bytes],
                files: Optional[List[dict]] = None):
    """Sends one HTML email as EMAIL_FROM_ADDRESS; images are referenced
    from the body as cid:<name>, and files ({name, type, data}) are
    attached. Raises HTTPException on failure."""
    attachments = [{"@odata.type": "#microsoft.graph.fileAttachment", "name": f"{cid}.png",
                    "contentType": "image/png", "contentBytes": base64.b64encode(png).decode(),
                    "isInline": True, "contentId": cid}
                   for cid, png in inline_images.items()]
    attachments += [{"@odata.type": "#microsoft.graph.fileAttachment", "name": f["name"],
                     "contentType": f["type"], "contentBytes": base64.b64encode(f["data"]).decode()}
                    for f in files or []]
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
                    "attachments": attachments,
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
    if not _graph_ready():
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
    with _lock, get_db() as conn:
        conn.execute("UPDATE extensions SET setup_email = ? WHERE ext = ? AND tenant_id = ?",
                     (to, ext, current_tenant()))  # offered next time
        conn.commit()
    changes_log.info(f"Emailed {'+'.join(apps)} setup for extension {ext} to {to}")
    return {"ok": True, "expires_at": expires.isoformat(),
            "messages": [f"Setup email sent to {to}. The link works until {_local_time(expires)}."]}


# ---------------------------------------------------------------------------
# Voicemail by email
#
# A mailbox with an email address (set on the extension's setup card) gets
# it in voicemail_portal.conf, so Asterisk writes a "new voicemail" email
# with the recording attached. Asterisk has no mail server to give it to
# here, so its mailcmd is asterisk/vm-mail.sh, which only drops the email
# in VM_OUTBOX. This loop sends each one on through Microsoft Graph, to
# the address Asterisk addressed it to, and only if that is a mailbox's
# address in pbx.db, so nothing else that can write to the folder can use
# it to send mail. Files that can't be sent are retried, and given up on
# (and logged) after a day.
# ---------------------------------------------------------------------------

# Graph refuses sendMail requests over 4 MB, and base64 adds a third.
VM_MAX_ATTACHMENT = int(2.5 * 1024 * 1024)
VM_GIVE_UP_AFTER = 24 * 3600
# Names already sent, so a file that couldn't be deleted is never sent twice.
_vm_sent: set = set()
_vm_warned = [False]


# voicemail.conf [general] sets Asterisk's email text to "Key: value"
# lines (see the README), so the details don't depend on its default
# wording; its X-Asterisk-VM-* headers are only added with IMAP storage:
#   emailbody=Mailbox: ${VM_MAILBOX}\nCaller-Name: ${VM_CIDNAME}\nCaller-Number: ${VM_CIDNUM}\nLength: ${VM_DUR}\nWhen: ${VM_DATE}\n


def _voicemail_email(raw: bytes):
    """(to, subject, html, files) for the email Asterisk wrote."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    to = email.utils.parseaddr(str(msg.get("To", "")))[1]
    text = msg.get_body(preferencelist=("plain",))
    fields = {}
    for line in (text.get_content() if text else "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and key.strip() not in fields:
            fields[key.strip()] = value.strip()
    header = {"Mailbox": "Mailbox", "Caller-Name": "Caller-ID-Name", "Caller-Number": "Caller-ID-Num",
              "When": "Orig-date", "Length": "Duration"}
    h = lambda name: fields.get(name) or str(msg.get(f"X-Asterisk-VM-{header[name]}", "")).strip()
    known = lambda v: v if v.lower() not in ("", "unknown", "anonymous") else ""
    name, number = known(h("Caller-Name")), known(h("Caller-Number"))
    caller = (f"{name} ({number})" if name and number else name or number) or "Unknown caller"
    files, too_big = [], False
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        if len(data) > VM_MAX_ATTACHMENT:
            too_big = True
            continue
        files.append({"name": part.get_filename() or "voicemail.wav", "type": part.get_content_type(),
                      "data": data})
    length = h("Length")
    if length.isdigit():  # the X-Asterisk-VM header gives plain seconds
        length = f"{int(length) // 60}:{int(length) % 60:02d}"
    rows = [("From", caller), ("Mailbox", h("Mailbox")), ("When", h("When")), ("Length", length)]
    esc = html.escape
    table = "".join(f"<tr><td style='color:#666;padding:2px 12px 2px 0'>{k}</td><td>{esc(v)}</td></tr>"
                    for k, v in rows if v)
    listen = ("The recording was too long to attach." if too_big else
              "The recording is attached." if files else "")
    body = f"""<div style="font:15px -apple-system,Segoe UI,sans-serif;color:#222;max-width:560px">
<p>You have a new voicemail.</p>
<table>{table}</table>
<p>{listen} You can also dial <b>{VOICEMAIL_NUMBER}</b> from your phone app to listen.</p>
</div>"""
    return to, f"New voicemail from {caller}", body, files


def send_voicemail_emails():
    """One pass over VM_OUTBOX. Returns how many emails were sent."""
    if not _graph_ready() or not os.path.isdir(VM_OUTBOX):
        return 0
    if not os.access(VM_OUTBOX, os.W_OK | os.X_OK):
        # Couldn't delete what it sends (e.g. the unit's ReadWritePaths
        # was skipped), so send nothing rather than resend forever.
        if not _vm_warned[0]:
            changes_log.info(f"Voicemail emails not sent: {VM_OUTBOX} isn't writable by the dashboard")
            _vm_warned[0] = True
        return 0
    _vm_warned[0] = False
    with get_db() as conn:
        allowed = {r["vm_email"].lower() for r in conn.execute("SELECT vm_email FROM extensions WHERE vm_email != ''")}
    sent = 0
    for name in sorted(os.listdir(VM_OUTBOX)):
        if not name.endswith(".eml") or name in _vm_sent:
            continue  # .in.* files are still being written
        path = os.path.join(VM_OUTBOX, name)
        try:
            with open(path, "rb") as f:
                to, subject, body, files = _voicemail_email(f.read())
            if to.lower() not in allowed:
                changes_log.info(f"Dropped a voicemail email to {to or '(no address)'}: not a mailbox's address")
                os.remove(path)
                continue
            _graph_send(to, subject, body, {}, files)
            _vm_sent.add(name)
            os.remove(path)
            sent += 1
            changes_log.info(f"Emailed a voicemail to {to}")
        except HTTPException as e:
            if time.time() - os.path.getmtime(path) > VM_GIVE_UP_AFTER:
                changes_log.info(f"Gave up emailing voicemail {name}: {e.detail}")
                os.remove(path)
        except Exception as e:
            changes_log.info(f"Couldn't read voicemail email {name}: {type(e).__name__}; removed")
            try:
                os.remove(path)
            except OSError:
                pass
    return sent


def _apply_now(reason: str) -> dict:
    with _lock, get_db() as conn:
        return apply_config(conn, reason)


async def apply_on_startup(attempts: int = 6, wait: int = 10):
    """Rewrite the Asterisk files and reload once when the dashboard starts,
    so a deploy that changes what's generated (new contexts, hints...) takes
    effect with the restart alone, no save needed. After a reboot Asterisk
    may still be starting, so it tries again for about a minute; a file
    that can't be written won't fix itself, so that stops at once."""
    import asyncio
    for attempt in range(attempts):
        try:
            result = await asyncio.to_thread(_apply_now, "Dashboard started: rewrote and reloaded")
        except Exception as e:  # e.g. pbx.db unreadable: the tab will say so
            changes_log.info(f"Dashboard started: couldn't apply the config: {e}")
            return
        if result["ok"] or any("couldn't write" in m for m in result["messages"]):
            return
        await asyncio.sleep(wait)


def take_phone_pins() -> int:
    """One pass over VM_PINS. Each file is removed whatever happens, and
    the PIN itself is never logged. Returns how many were saved."""
    if not os.path.isdir(VM_PINS) or not os.access(VM_PINS, os.W_OK | os.X_OK):
        return 0
    saved = 0
    for name in sorted(os.listdir(VM_PINS)):
        if not name.endswith(".pin"):
            continue  # .in.* files are still being written
        path = os.path.join(VM_PINS, name)
        try:
            with open(path) as f:
                parts = f.read(64).split()
        except OSError:
            parts = []
        try:
            os.remove(path)
        except OSError:
            pass
        if len(parts) != 3:
            changes_log.info(f"Ignored a voicemail PIN file from Asterisk ({name}): not readable")
            continue
        context, mailbox, pin = parts
        with _lock, get_db() as conn:
            tenant = next((t for t in all_tenant_names(conn) if t.vm == context), None)
            row = tenant and conn.execute("SELECT 1 FROM extensions WHERE tenant_id = ? AND ext = ?",
                                          (tenant.id, mailbox)).fetchone()
            if not row:
                changes_log.info(f"Ignored a voicemail PIN change from a phone for {mailbox}@{context}: no such mailbox")
                continue
            try:
                pin = _clean_pin(pin, mailbox)
            except HTTPException:
                changes_log.info(f"Ignored a voicemail PIN change from a phone for {mailbox}@{context}: "
                                 "too easy to guess")
                continue
            conn.execute("UPDATE extensions SET vm_pin = ?, updated_at = ? WHERE tenant_id = ? AND ext = ?",
                         (pin, _now(), tenant.id, mailbox))
            conn.commit()
            apply_config(conn, f"Voicemail PIN for extension {mailbox} changed from the phone"
                               + ("" if tenant.id == MAIN_TENANT else f" (tenant {tenant.code})"))
            saved += 1
    return saved


async def vm_pin_loop():
    import asyncio
    while True:
        try:
            await asyncio.to_thread(take_phone_pins)
        except Exception:
            pass
        await asyncio.sleep(3)


async def voicemail_mail_loop():
    import asyncio
    while True:
        try:
            await asyncio.to_thread(send_voicemail_emails)
        except Exception:
            pass
        await asyncio.sleep(15)
