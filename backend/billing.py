"""
Phone billing — the Phone System tab's Billing section (/api/billing/*).

Stage 1: calls and their cost.
- Calls come from Asterisk's CDR log (cdr-csv/Master.csv), read every
  minute and kept in billing.db. Re-reading the file is harmless: a call
  is keyed on (uniqueid, dstchannel, start).
- Each trunk (and each Voxbeam route, which has its own prices) gets rate
  sheets uploaded from the provider's own spreadsheet. A sheet is kept as
  a version, and a call is priced from the sheet in force when it was made.
- An outbound call is priced by the longest matching prefix, with that
  row's billing increments, and converted to euro at the ECB rate for the
  call's day. Every call keeps its full pricing trace.

Money is in integer micro-units (millionths of the currency) throughout,
never floats. A call is one of:
  rated        priced from a rate valid at the time of the call
  provisional  priced from the latest rate, but the call is after that
               rate's expiry: upload the provider's newer sheet
  unrated      couldn't be priced (no sheet, no matching prefix, no
               exchange rate yet): never shown as zero
  free         not billable: internal, inbound, unanswered, emergency,
               voicemail
Calls on an issued statement (statement_id, stage 2) are never re-priced.
"""

import base64
import csv
import io
import os
import re
import sqlite3
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from pbx import TRUNKS, VOXBEAM_ROUTES, changes_log

BILLING_DB = os.environ.get("PBX_BILLING_DB", "/var/lib/admin-dashboard/billing.db")
CDR_CSV = os.environ.get("PBX_CDR_CSV", "/var/log/asterisk/cdr-csv/Master.csv")
# Asterisk writes CDR times in its own zone (UTC on this VPS; cdr.conf
# usegmtime). Statements and months are in local (Irish) time.
CDR_TZ = ZoneInfo(os.environ.get("PBX_CDR_TIMEZONE", "UTC"))
LOCAL_TZ = ZoneInfo(os.environ.get("PBX_TIMEZONE", "Europe/Dublin"))
FX_URL = os.environ.get("PBX_FX_URL", "https://api.frankfurter.dev/v1")  # ECB rates, as Money Mapper
MAX_SHEET_BYTES = 20 * 1024 * 1024

MICRO = Decimal(1_000_000)
# Rows whose status says they're gone aren't imported.
DROPPED_STATUSES = ("delet", "remov", "closed", "discontinu", "blocked")

_lock = threading.Lock()
router = APIRouter(prefix="/api/billing")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(BILLING_DB) or ".", exist_ok=True)
    fresh = not os.path.exists(BILLING_DB)
    conn = sqlite3.connect(BILLING_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS calls (
               id INTEGER PRIMARY KEY,
               uniqueid TEXT NOT NULL, dstchannel TEXT NOT NULL, start_utc TEXT NOT NULL,
               answer_utc TEXT, end_utc TEXT, duration INTEGER, billsec INTEGER, disposition TEXT,
               direction TEXT, ext TEXT, trunk TEXT, route TEXT, dialled TEXT, number TEXT,
               caller TEXT, channel TEXT, lastdata TEXT,
               status TEXT NOT NULL DEFAULT 'unrated', reason TEXT,
               sheet_id INTEGER, prefix TEXT, destination TEXT, rate_micro INTEGER, currency TEXT,
               bill_seconds INTEGER, conn_fee_micro INTEGER, cost_micro INTEGER,
               fx_rate TEXT, fx_date TEXT, cost_eur_micro INTEGER,
               statement_id INTEGER, rated_at TEXT,
               UNIQUE (uniqueid, dstchannel, start_utc));
           CREATE INDEX IF NOT EXISTS calls_start ON calls (start_utc);
           CREATE TABLE IF NOT EXISTS rate_sheets (
               id INTEGER PRIMARY KEY,
               trunk TEXT NOT NULL, route TEXT NOT NULL DEFAULT '',
               filename TEXT, uploaded_at TEXT NOT NULL, rows INTEGER,
               valid_from TEXT, valid_to TEXT, currencies TEXT);
           CREATE TABLE IF NOT EXISTS rates (
               sheet_id INTEGER NOT NULL, prefix TEXT NOT NULL, destination TEXT,
               rate_micro INTEGER NOT NULL, rate_before_micro INTEGER, change_at TEXT,
               expires_at TEXT, currency TEXT NOT NULL,
               conn_unit INTEGER NOT NULL, bill_unit INTEGER NOT NULL, conn_fee_micro INTEGER NOT NULL);
           CREATE INDEX IF NOT EXISTS rates_lookup ON rates (sheet_id, prefix);
           CREATE TABLE IF NOT EXISTS mappings (trunk TEXT PRIMARY KEY, mapping TEXT NOT NULL);
           CREATE TABLE IF NOT EXISTS fx (day TEXT NOT NULL, currency TEXT NOT NULL, rate TEXT NOT NULL,
               PRIMARY KEY (day, currency));
           CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);"""
    )
    if fresh:
        os.chmod(BILLING_DB, 0o600)
    return conn


def _state(conn, key, default=""):
    r = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def _set_state(conn, key, value):
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_micro(value) -> int:
    return int((Decimal(value) * MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_micro(micro: Optional[int], places: int = 4) -> Optional[str]:
    if micro is None:
        return None
    return str((Decimal(micro) / MICRO).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Reading Asterisk's CDR log
#
# cdr_csv columns: accountcode, src, dst, dcontext, clid, channel,
# dstchannel, lastapp, lastdata, start, answer, end, duration, billsec,
# disposition, amaflags, uniqueid, userfield. Note that src is the caller
# ID shown, which for outbound calls is the company number, not the
# extension: the extension comes from the channel (PJSIP/202-0000005a).
# ---------------------------------------------------------------------------

CDR_FIELDS = ["accountcode", "src", "dst", "dcontext", "clid", "channel", "dstchannel", "lastapp", "lastdata",
              "start", "answer", "end", "duration", "billsec", "disposition", "amaflags", "uniqueid", "userfield"]
TRUNK_CHANNEL = re.compile(r"^PJSIP/(%s)-" % "|".join(map(re.escape, TRUNKS)))
EXT_CHANNEL = re.compile(r"^PJSIP/(\d+)-")
DIAL_TARGET = re.compile(r"^PJSIP/([^@,&]+)@([A-Za-z0-9_-]+)")


def _cdr_time(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CDR_TZ).astimezone(timezone.utc) \
            .isoformat(timespec="seconds")
    except ValueError:
        return None


def normalise_number(trunk: str, sent: str):
    """(international number without +, Voxbeam route, kind or None) for the
    number a trunk was sent; kind 'emergency' is never charged and a None
    number means it can't be priced."""
    digits = sent.strip()
    if digits in ("112", "999"):
        return digits, "", "emergency"
    route = ""
    if trunk == "voxbeam":
        route = digits[:7]
        if route not in VOXBEAM_ROUTES:
            return None, "", None
        return digits[7:] or None, route, None
    digits = digits.lstrip("+")
    if digits.startswith("00"):
        return digits[2:] or None, route, None
    if digits.startswith("1800") or digits.startswith("1850") or digits.startswith("1890"):
        return "353" + digits, route, None  # Irish freephone/LoCall, not North America's 1-800
    if digits.startswith("0"):
        return "353" + digits[1:], route, None
    return digits or None, route, None


def classify(row: dict) -> dict:
    """The call as billing sees it, from one CDR row."""
    channel, dstchannel = row["channel"], row["dstchannel"]
    trunk_out = TRUNK_CHANNEL.match(dstchannel or "")
    trunk_in = TRUNK_CHANNEL.match(channel or "")
    ext = EXT_CHANNEL.match(channel or "")
    call = {
        "uniqueid": row["uniqueid"], "dstchannel": dstchannel or "", "start_utc": _cdr_time(row["start"]),
        "answer_utc": _cdr_time(row["answer"]), "end_utc": _cdr_time(row["end"]),
        "duration": int(row["duration"] or 0), "billsec": int(row["billsec"] or 0),
        "disposition": row["disposition"], "dialled": row["dst"], "caller": row["clid"] or row["src"],
        "channel": channel, "lastdata": row["lastdata"],
        "ext": ext.group(1) if ext else None, "trunk": None, "route": "", "number": None,
    }
    if trunk_out:
        call["direction"] = "out"
        call["trunk"] = trunk_out.group(1)
        target = DIAL_TARGET.match(row["lastdata"] or "")
        if target and target.group(2) == call["trunk"]:
            call["number"], call["route"], kind = normalise_number(call["trunk"], target.group(1))
            if kind == "emergency":
                call["status"], call["reason"] = "free", "Emergency call"
        elif (row["dst"] or "") in ("112", "999"):
            call["number"], call["status"], call["reason"] = row["dst"], "free", "Emergency call"
    elif trunk_in:
        call["direction"], call["trunk"] = "in", trunk_in.group(1)
        call["number"] = (row["dst"] or "").lstrip("+")
        target_ext = EXT_CHANNEL.match(dstchannel or "")
        call["ext"] = target_ext.group(1) if target_ext else None
    else:
        call["direction"] = "internal"
    if call.get("status") != "free":
        if call["direction"] != "out":
            call["status"], call["reason"] = "free", "Inbound call" if call["direction"] == "in" else "Internal call"
        elif row["disposition"] != "ANSWERED" or call["billsec"] <= 0:
            call["status"], call["reason"] = "free", f"Not answered ({row['disposition'].lower()})"
        elif not call["number"]:
            call["status"], call["reason"] = "unrated", "Couldn't read the number dialled"
        else:
            call["status"], call["reason"] = "unrated", "Not priced yet"
    return call


def import_cdr() -> int:
    """Reads new lines from the CDR log. Returns how many calls were added."""
    if not os.path.exists(CDR_CSV):
        return 0
    with _lock, get_db() as conn:
        st = os.stat(CDR_CSV)
        offset = int(_state(conn, "cdr_offset", "0") or 0)
        if _state(conn, "cdr_inode") != str(st.st_ino) or st.st_size < offset:
            offset = 0  # rotated or emptied: read it all again (duplicates are ignored)
        added = 0
        with open(CDR_CSV, "rb") as f:
            f.seek(offset)
            data = f.read()
        complete = data[:data.rfind(b"\n") + 1]  # a half-written last line waits for next time
        for row in csv.reader(io.StringIO(complete.decode("utf-8", "replace"))):
            if len(row) < len(CDR_FIELDS):
                continue
            call = classify(dict(zip(CDR_FIELDS, row)))
            if not call["start_utc"]:
                continue
            cols = list(call)
            cur = conn.execute(f"INSERT OR IGNORE INTO calls ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [call[c] for c in cols])
            added += cur.rowcount
        _set_state(conn, "cdr_offset", offset + len(complete))
        _set_state(conn, "cdr_inode", st.st_ino)
        _set_state(conn, "cdr_read_at", _now())
        conn.commit()
    return added


# ---------------------------------------------------------------------------
# Exchange rates (ECB, via Frankfurter), one per currency per day
# ---------------------------------------------------------------------------

def fx_to_eur(conn, currency: str, day: date):
    """(rate, the ECB date it's from) to turn `currency` into euro, or None."""
    if currency == "EUR":
        return Decimal(1), day.isoformat()
    key = day.isoformat()
    r = conn.execute("SELECT rate FROM fx WHERE day = ? AND currency = ?", (key, currency)).fetchone()
    if r:
        rate, src_day = r["rate"].split("@")
        return Decimal(rate), src_day
    if day > datetime.now(LOCAL_TZ).date():
        return None
    try:
        res = httpx.get(f"{FX_URL}/{key}", params={"base": currency, "symbols": "EUR"}, timeout=10)
        res.raise_for_status()
        data = res.json()
        rate, src_day = Decimal(str(data["rates"]["EUR"])), data.get("date", key)
    except (httpx.HTTPError, KeyError, ValueError, InvalidOperation):
        return None
    # Weekends and holidays get the last business day's rate (Frankfurter
    # says which); today's rate is published mid-afternoon, so don't keep a
    # stand-in for today.
    if src_day == key or day < datetime.now(LOCAL_TZ).date():
        conn.execute("INSERT OR REPLACE INTO fx (day, currency, rate) VALUES (?, ?, ?)", (key, currency, f"{rate}@{src_day}"))
    return rate, src_day


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def bill_seconds(billsec: int, conn_unit: int, bill_unit: int) -> int:
    """Seconds charged: the first conn_unit seconds, then bill_unit blocks
    (1/1 is per second, 60/60 per minute, 30/6 a 30 s minimum then 6 s)."""
    conn_unit, bill_unit = max(conn_unit, 1), max(bill_unit, 1)
    if billsec <= conn_unit:
        return conn_unit
    return conn_unit + -(-(billsec - conn_unit) // bill_unit) * bill_unit


def _sheet_for(conn, trunk: str, route: str, at: str):
    """The sheet in force at `at`: the newest one that had started by then,
    or, for a call before every sheet, the oldest (using its old rates)."""
    sheets = conn.execute("SELECT * FROM rate_sheets WHERE trunk = ? AND route = ? ORDER BY uploaded_at DESC, id DESC",
                          (trunk, route)).fetchall()
    for s in sheets:
        if not s["valid_from"] or s["valid_from"] <= at:
            return s
    return sheets[-1] if sheets else None


def price_call(conn, call) -> dict:
    """The pricing fields for one unfrozen outbound call."""
    out = {k: None for k in ("sheet_id", "prefix", "destination", "rate_micro", "currency", "bill_seconds",
                             "conn_fee_micro", "cost_micro", "fx_rate", "fx_date", "cost_eur_micro")}
    out["rated_at"] = _now()
    if call["status"] == "free":
        return {**out, "status": "free", "reason": call["reason"]}
    if not call["number"]:
        return {**out, "status": "unrated", "reason": "Couldn't read the number dialled"}
    route_name = f" {VOXBEAM_ROUTES.get(call['route'], call['route'])}" if call["route"] else ""
    sheet = _sheet_for(conn, call["trunk"], call["route"] or "", call["start_utc"])
    if not sheet:
        return {**out, "status": "unrated",
                "reason": f"No rate sheet for {TRUNKS[call['trunk']]['label']}{route_name}"}
    candidates = [call["number"][:i] for i in range(len(call["number"]), 0, -1)]
    row = conn.execute(f"SELECT * FROM rates WHERE sheet_id = ? AND prefix IN ({','.join('?' * len(candidates))}) "
                       "ORDER BY length(prefix) DESC LIMIT 1", [sheet["id"], *candidates]).fetchone()
    if not row:
        return {**out, "sheet_id": sheet["id"], "status": "unrated", "reason": "No prefix in the rate sheet matches"}
    at = call["start_utc"]
    rate = row["rate_micro"]
    if row["change_at"] and at < row["change_at"] and row["rate_before_micro"] is not None:
        rate = row["rate_before_micro"]  # the "existing rate", before the announced change
    secs = bill_seconds(call["billsec"], row["conn_unit"], row["bill_unit"])
    cost = int((Decimal(rate) * secs / 60).quantize(Decimal(1), rounding=ROUND_CEILING)) + row["conn_fee_micro"]
    out.update(sheet_id=sheet["id"], prefix=row["prefix"], destination=row["destination"], rate_micro=rate,
               currency=row["currency"], bill_seconds=secs, conn_fee_micro=row["conn_fee_micro"], cost_micro=cost)
    local_day = datetime.fromisoformat(at).astimezone(LOCAL_TZ).date()
    fx = fx_to_eur(conn, row["currency"], local_day)
    if not fx:
        return {**out, "status": "unrated", "reason": f"No {row['currency']}→EUR exchange rate for {local_day} yet"}
    out.update(fx_rate=str(fx[0]), fx_date=fx[1],
               cost_eur_micro=int((Decimal(cost) * fx[0]).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    expired = row["expires_at"] and at >= row["expires_at"]
    return {**out, "status": "provisional" if expired else "rated",
            "reason": "After this rate's expiry: upload the newer sheet" if expired else None}


def rate_calls(where: str = "status = 'unrated'", args=()) -> int:
    """Prices the unfrozen outbound calls matching `where`. Returns how many
    now have a price."""
    with _lock, get_db() as conn:
        rows = conn.execute(f"SELECT * FROM calls WHERE statement_id IS NULL AND direction = 'out' AND ({where})",
                            args).fetchall()
        changed = 0
        for call in rows:
            # A call marked free when imported (unanswered, emergency) stays free.
            fields = price_call(conn, call)
            conn.execute(f"UPDATE calls SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         [*fields.values(), call["id"]])
            changed += fields["status"] in ("rated", "provisional")
        conn.commit()
    return changed


def billing_pass():
    """Import new calls and price whatever isn't priced yet."""
    import_cdr()
    rate_calls()


async def billing_loop():
    import asyncio
    while True:
        try:
            await asyncio.to_thread(billing_pass)
        except Exception as e:
            changes_log.info(f"Billing pass failed: {type(e).__name__}: {e}")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Rate sheets: reading the provider's own spreadsheet
#
# The upload is previewed first: the columns are shown with a guess at
# which is which, and the guess (or the user's correction) is remembered
# per trunk, so next time it's just "upload".
# ---------------------------------------------------------------------------

FIELDS = {  # field: (what it is, required, header words that suggest it)
    "prefix": ("Prefix / dial code", True, ("prefix", "dial code", "code", "dialcode")),
    "destination": ("Destination name", False, ("route name", "destination", "description", "country", "name")),
    "rate": ("Rate per minute", True, ("new rate", "rate", "price", "cost", "per min")),
    "rate_before": ("Rate before the change (optional)", False, ("existing rate", "old rate", "current rate", "previous rate")),
    "currency": ("Currency", False, ("currency", "curr")),
    "conn_unit": ("First increment, seconds", False, ("connection unit", "min duration", "initial", "first increment")),
    "bill_unit": ("Next increments, seconds", False, ("bill unit", "billing unit", "increment", "next increment")),
    "conn_fee": ("Connection fee", False, ("connection fee", "connection charge", "setup fee", "connect fee")),
    "effective_date": ("Change date", False, ("effective date", "effective from", "valid from", "start date")),
    "effective_time": ("Change time", False, ("effective time",)),
    "expiry_date": ("Expiry date", False, ("expiry date", "expires", "valid to", "end date")),
    "expiry_time": ("Expiry time", False, ("expiry time",)),
    "status": ("Status", False, ("status",)),
}


def read_sheet(content: bytes, filename: str) -> List[List[str]]:
    """All rows as strings, header first, from CSV/TSV/semicolon or Excel."""
    if len(content) > MAX_SHEET_BYTES:
        raise HTTPException(400, "That file is over 20 MB.")
    if content[:2] == b"PK":  # .xlsx is a zip
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            rows = [[_cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
        except Exception as e:
            raise HTTPException(400, f"Couldn't read that Excel file: {type(e).__name__}")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        sample = text[:20000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [[c.strip() for c in r] for r in rows if any((c or "").strip() for c in r)]
    if len(rows) < 2:
        raise HTTPException(400, "That file has no rows under its header.")
    return rows


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dtime):
        return v.strftime("%H:%M:%S")
    if isinstance(v, float):
        return format(Decimal(repr(v)), "f")
    return str(v)


def guess_mapping(header: List[str]) -> Dict[str, Optional[int]]:
    low = [h.lower().strip() for h in header]
    mapping, used = {}, set()
    for field, (_, _, words) in FIELDS.items():
        mapping[field] = None
        for word in words:  # earlier words are better guesses
            hit = next((i for i, h in enumerate(low) if i not in used and (h == word or word in h)), None)
            if hit is not None:
                mapping[field] = hit
                used.add(hit)
                break
    return mapping


DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d-%B-%Y")


def _parse_datetime(day: str, clock: str = "") -> Optional[str]:
    """A sheet's date (+ time) as UTC ISO; providers publish in UTC/GMT."""
    day = (day or "").strip()
    if not day:
        return None
    if " " in day and not clock:
        day, clock = day.split(" ", 1)
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(day, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unknown date '{day}'")
    t = dtime(0, 0)
    if clock.strip():
        t = datetime.strptime(clock.strip()[:8], "%H:%M:%S" if clock.count(":") == 2 else "%H:%M").time()
    return datetime.combine(d, t, tzinfo=timezone.utc).isoformat(timespec="seconds")


def _money(value: str) -> Decimal:
    v = (value or "").strip().replace("€", "").replace("$", "").replace(" ", "")
    if "," in v and "." not in v:
        v = v.replace(",", ".")
    return Decimal(v)


class SheetDefaults(BaseModel):
    currency: str = "EUR"
    conn_unit: int = 1
    bill_unit: int = 1
    conn_fee: str = "0"


def parse_rates(rows, mapping: Dict[str, Optional[int]], defaults: SheetDefaults):
    """(rates, stats) for a sheet read with `mapping` (field: column index)."""
    for field, (label, required, _) in FIELDS.items():
        if required and mapping.get(field) is None:
            raise HTTPException(400, f"Choose the column for '{label}'.")
    col = lambda r, f: r[mapping[f]] if mapping.get(f) is not None and mapping[f] < len(r) else ""
    rates, stats = {}, {"rows": 0, "imported": 0, "skipped": 0, "errors": [], "statuses": {}, "currencies": {},
                        "increments": {}, "change_dates": [], "expiry_dates": []}
    for n, r in enumerate(rows[1:], start=2):
        stats["rows"] += 1
        status = col(r, "status")
        stats["statuses"][status or "(none)"] = stats["statuses"].get(status or "(none)", 0) + 1
        if status and any(word in status.lower() for word in DROPPED_STATUSES):
            stats["skipped"] += 1
            continue
        try:
            prefix = re.sub(r"\D", "", col(r, "prefix"))
            if not prefix:
                raise ValueError("no prefix")
            rate = to_micro(_money(col(r, "rate")))
            before = col(r, "rate_before")
            currency = (col(r, "currency") or defaults.currency).upper()[:3]
            conn_unit = int(Decimal(col(r, "conn_unit") or defaults.conn_unit))
            bill_unit = int(Decimal(col(r, "bill_unit") or defaults.bill_unit))
            fee = col(r, "conn_fee")
            change_at = _parse_datetime(col(r, "effective_date"), col(r, "effective_time"))
            expires_at = _parse_datetime(col(r, "expiry_date"), col(r, "expiry_time"))
            rates[prefix] = (prefix, col(r, "destination")[:80], rate, to_micro(_money(before)) if before else None,
                             change_at, expires_at, currency, conn_unit, bill_unit,
                             to_micro(_money(fee) if fee else _money(defaults.conn_fee)))
        except (ValueError, InvalidOperation) as e:
            stats["skipped"] += 1
            if len(stats["errors"]) < 5:
                stats["errors"].append(f"Line {n}: {e}")
            continue
        stats["currencies"][currency] = stats["currencies"].get(currency, 0) + 1
        inc = f"{conn_unit}/{bill_unit}"
        stats["increments"][inc] = stats["increments"].get(inc, 0) + 1
        if change_at:
            stats["change_dates"].append(change_at)
        if expires_at:
            stats["expiry_dates"].append(expires_at)
    stats["imported"] = len(rates)
    for k in ("change_dates", "expiry_dates"):
        stats[k] = [min(stats[k]), max(stats[k])] if stats[k] else []
    return list(rates.values()), stats


class SheetRequest(BaseModel):
    trunk: str
    route: str = ""
    filename: str
    content_b64: str
    mapping: Optional[Dict[str, Optional[int]]] = None
    defaults: SheetDefaults = SheetDefaults()


def _read_request(req: SheetRequest):
    if req.trunk not in TRUNKS:
        raise HTTPException(400, "Unknown trunk.")
    if req.trunk == "voxbeam" and req.route not in VOXBEAM_ROUTES:
        raise HTTPException(400, "Choose which Voxbeam route this sheet is for.")
    if req.trunk != "voxbeam" and req.route:
        raise HTTPException(400, f"{TRUNKS[req.trunk]['label']} has no routes.")
    try:
        content = base64.b64decode(req.content_b64, validate=True)
    except ValueError:
        raise HTTPException(400, "The file didn't upload properly.")
    return read_sheet(content, req.filename)


@router.post("/rate-sheets/preview")
def api_preview_sheet(req: SheetRequest):
    rows = _read_request(req)
    mapping = req.mapping
    if mapping is None:
        with get_db() as conn:
            saved = conn.execute("SELECT mapping FROM mappings WHERE trunk = ?", (req.trunk,)).fetchone()
        import json
        mapping = json.loads(saved["mapping"]) if saved else guess_mapping(rows[0])
        if saved and any(v is not None and v >= len(rows[0]) for v in mapping.values()):
            mapping = guess_mapping(rows[0])  # this sheet's layout differs from last time
    try:
        _, stats = parse_rates(rows, mapping, req.defaults)
    except HTTPException as e:
        stats = {"problem": e.detail}
    return {"columns": rows[0], "sample": rows[1:6], "mapping": mapping, "stats": stats,
            "fields": {k: {"label": v[0], "required": v[1]} for k, v in FIELDS.items()}}


@router.post("/rate-sheets")
def api_import_sheet(req: SheetRequest):
    import json
    rows = _read_request(req)
    if req.mapping is None:
        raise HTTPException(400, "Preview the sheet first.")
    rates, stats = parse_rates(rows, req.mapping, req.defaults)
    if not rates:
        raise HTTPException(400, "No rates could be read with those columns.")
    with _lock, get_db() as conn:
        valid_from = stats["change_dates"][0] if stats["change_dates"] else _now()
        valid_to = stats["expiry_dates"][1] if stats["expiry_dates"] else None
        cur = conn.execute("INSERT INTO rate_sheets (trunk, route, filename, uploaded_at, rows, valid_from, valid_to, "
                           "currencies) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (req.trunk, req.route, req.filename[:120], _now(), len(rates), valid_from, valid_to,
                            ",".join(sorted(stats["currencies"]))))
        sheet_id = cur.lastrowid
        conn.executemany("INSERT INTO rates (sheet_id, prefix, destination, rate_micro, rate_before_micro, change_at, "
                         "expires_at, currency, conn_unit, bill_unit, conn_fee_micro) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(sheet_id, *r) for r in rates])
        conn.execute("INSERT INTO mappings (trunk, mapping) VALUES (?, ?) ON CONFLICT(trunk) DO UPDATE SET "
                     "mapping = excluded.mapping", (req.trunk, json.dumps(req.mapping)))
        conn.commit()
    changes_log.info(f"Uploaded rate sheet {req.filename} for {req.trunk} {req.route}: {len(rates)} rates")
    repriced = rate_calls("status IN ('unrated', 'provisional', 'rated') AND trunk = ? AND route = ?",
                          (req.trunk, req.route))
    return {"ok": True, "sheet_id": sheet_id, "stats": stats,
            "messages": [f"{len(rates):,} rates imported. {repriced} call(s) re-priced."]}


@router.delete("/rate-sheets/{sheet_id}")
def api_delete_sheet(sheet_id: int):
    with _lock, get_db() as conn:
        s = conn.execute("SELECT * FROM rate_sheets WHERE id = ?", (sheet_id,)).fetchone()
        if not s:
            raise HTTPException(404, "Rate sheet not found.")
        if conn.execute("SELECT 1 FROM calls WHERE sheet_id = ? AND statement_id IS NOT NULL", (sheet_id,)).fetchone():
            raise HTTPException(409, "Calls on an issued statement were priced from this sheet.")
        conn.execute("DELETE FROM rates WHERE sheet_id = ?", (sheet_id,))
        conn.execute("DELETE FROM rate_sheets WHERE id = ?", (sheet_id,))
        conn.commit()
    changes_log.info(f"Deleted rate sheet {sheet_id} ({s['filename']})")
    repriced = rate_calls("trunk = ? AND route = ?", (s["trunk"], s["route"]))
    return {"ok": True, "messages": [f"Rate sheet deleted. {repriced} call(s) re-priced."]}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _month_range(month: str):
    """UTC bounds of a local calendar month 'YYYY-MM'."""
    try:
        y, m = map(int, month.split("-"))
        start = datetime(y, m, 1, tzinfo=LOCAL_TZ)
    except ValueError:
        raise HTTPException(400, "Month must look like 2026-10.")
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=LOCAL_TZ)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds"), \
        end.astimezone(timezone.utc).isoformat(timespec="seconds")


def _route_label(c) -> str:
    label = TRUNKS.get(c["trunk"], {}).get("label", c["trunk"] or "")
    return f"{label} {VOXBEAM_ROUTES[c['route']]}" if c["route"] in VOXBEAM_ROUTES else label


def _call_json(c) -> dict:
    local = datetime.fromisoformat(c["start_utc"]).astimezone(LOCAL_TZ)
    return {
        "id": c["id"], "start": local.strftime("%Y-%m-%d %H:%M:%S"), "direction": c["direction"], "ext": c["ext"],
        "trunk": _route_label(c), "number": c["number"], "dialled": c["dialled"], "caller": c["caller"],
        "billsec": c["billsec"], "disposition": c["disposition"], "status": c["status"], "reason": c["reason"],
        "destination": c["destination"], "prefix": c["prefix"],
        "rate": from_micro(c["rate_micro"], 6), "currency": c["currency"], "bill_seconds": c["bill_seconds"],
        "conn_fee": from_micro(c["conn_fee_micro"], 6), "cost": from_micro(c["cost_micro"], 6),
        "fx_rate": c["fx_rate"], "fx_date": c["fx_date"], "cost_eur": from_micro(c["cost_eur_micro"], 6),
        "sheet_id": c["sheet_id"],
    }


def _calls(month: str, direction: str = "", status: str = ""):
    start, end = _month_range(month)
    sql, args = "SELECT * FROM calls WHERE start_utc >= ? AND start_utc < ?", [start, end]
    if direction:
        sql, args = sql + " AND direction = ?", args + [direction]
    if status:
        sql, args = sql + " AND status = ?", args + [status]
    with get_db() as conn:
        return conn.execute(sql + " ORDER BY start_utc DESC", args).fetchall()


@router.get("/overview")
def api_overview(month: str = ""):
    month = month or datetime.now(LOCAL_TZ).strftime("%Y-%m")
    with get_db() as conn:
        sheets = [dict(s) for s in conn.execute("SELECT * FROM rate_sheets ORDER BY trunk, route, uploaded_at DESC")]
        read_at = _state(conn, "cdr_read_at")
    now = _now()
    for s in sheets:
        s["route_label"] = VOXBEAM_ROUTES.get(s["route"], "")
        s["expired"] = bool(s["valid_to"] and s["valid_to"] < now)
    calls = _calls(month)
    by_status = {}
    for c in calls:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    out = [c for c in calls if c["direction"] == "out"]
    return {
        "month": month, "cdr_file": CDR_CSV, "cdr_found": os.path.exists(CDR_CSV), "cdr_read_at": read_at,
        "sheets": sheets, "counts": by_status, "calls": len(calls), "outbound": len(out),
        "outbound_minutes": round(sum(c["billsec"] for c in out) / 60, 1),
        "cost_eur": from_micro(sum(c["cost_eur_micro"] or 0 for c in out), 2),
        "routes": [{"trunk": t, "route": "", "label": v["label"]} for t, v in TRUNKS.items() if t != "voxbeam"]
                  + [{"trunk": "voxbeam", "route": p, "label": f"Voxbeam {n}"} for p, n in VOXBEAM_ROUTES.items()],
    }


@router.get("/calls")
def api_calls(month: str = "", direction: str = "", status: str = ""):
    month = month or datetime.now(LOCAL_TZ).strftime("%Y-%m")
    return {"calls": [_call_json(c) for c in _calls(month, direction, status)[:2000]]}


@router.get("/calls.csv")
def api_calls_csv(month: str = "", direction: str = "", status: str = ""):
    month = month or datetime.now(LOCAL_TZ).strftime("%Y-%m")
    buf = io.StringIO()
    cols = ["start", "direction", "ext", "trunk", "number", "caller", "billsec", "disposition", "status", "reason",
            "destination", "prefix", "rate", "currency", "bill_seconds", "conn_fee", "cost", "fx_rate", "fx_date",
            "cost_eur"]
    w = csv.writer(buf)
    w.writerow(cols)
    for c in _calls(month, direction, status):
        j = _call_json(c)
        w.writerow([j[k] for k in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="calls-{month}.csv"'})


@router.post("/refresh")
def api_refresh():
    added = import_cdr()
    priced = rate_calls()
    return {"ok": True, "messages": [f"{added} new call(s) read, {priced} priced."]}


@router.post("/reprice")
def api_reprice(month: str):
    start, end = _month_range(month)
    n = rate_calls("start_utc >= ? AND start_utc < ?", (start, end))
    changes_log.info(f"Re-priced {n} call(s) for {month}")
    return {"ok": True, "messages": [f"{n} call(s) re-priced."]}
