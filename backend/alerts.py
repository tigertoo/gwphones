"""
Alert calls: another service on this machine (the pet tracker) asks for a
call to a mobile that speaks a short message, e.g. "Sydney's tag has just
been scanned". Two kinds:

- say: the message, twice, then goodbye.
- ask: the message (which ends "press 1 to ..."), up to three times; a 1
  is recorded, and the service asking reads it back (GET /alert/call/<id>).
  That's how the pet tracker checks a number belongs to whoever typed it
  in before it rings it with alerts.

The message is spoken by Piper, text to speech on this machine (nothing
leaves it), converted here to 8 kHz mu-law, and stored in
/etc/asterisk/portal/alerts/ (the folder's setgid bit gives it to the
asterisk group); files and records go after a day.

The call is started like click to call, from PBX_ALERT_EXT, an extension
of the tenant PBX_ALERT_TENANT (its code; blank: the main tenant). Best a
tenant of its own that only does this (one extension, no phone, no portal
users), so the calls have their own caller ID, call limit, price cap and
statement. Through that tenant's click to call [..-c2c-dial], so it shows
the extension's caller ID, goes through the route check and price cap and
is billed to it (CDR userfield c2c:<ext>). The answered side plays the
message from [portal-alert-say] or [portal-alert-ask], which nothing a
phone can dial reaches.

Only from this machine, never through nginx (refused when X-Real-IP or
X-Forwarded-For is present):
- POST /alert/call and GET /alert/call/<id>: with X-Alert-Token, the
  shared secret PBX_ALERT_TOKEN (unset: alert calls are off);
- GET /alert/pressed?id=: Asterisk itself, with the route check's token.
Irish mobiles only; at most PER_NUMBER_HOUR calls an hour to one number
and PER_HOUR in all, since each costs money.
"""

import array
import hmac
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import wave

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from pbx import (GENERATED_DIR, MAIN_TENANT, _lcr_token, asterisk_cli, changes_log, classify_dialled, get_db,
                 tenant_names)

public_router = APIRouter(prefix="/alert")

ALERT_TOKEN = os.environ.get("PBX_ALERT_TOKEN", "")
ALERT_EXT = os.environ.get("PBX_ALERT_EXT", "")
ALERT_TENANT = os.environ.get("PBX_ALERT_TENANT", "")  # a tenant's code; blank: the main tenant
TTS_MODEL = os.environ.get("PBX_TTS_MODEL", "")  # a Piper voice (.onnx), its .onnx.json next to it
ALERT_DIR = os.path.join(GENERATED_DIR, "alerts")
PRESSED_URL = os.environ.get("PBX_ALERT_PRESSED_URL", "http://127.0.0.1:8010/alert/pressed")
SAY_CONTEXT, ASK_CONTEXT = "portal-alert-say", "portal-alert-ask"
PER_NUMBER_HOUR, PER_HOUR = 6, 30
KEEP_SECONDS = 86400
TEXT_MAX = 300

SCHEMA = """CREATE TABLE IF NOT EXISTS alert_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,           -- say or ask
    number     TEXT NOT NULL,           -- as dialled (national)
    created_at REAL NOT NULL,
    pressed_at REAL
)"""


def alert_path(call_id: int) -> str:
    return os.path.join(ALERT_DIR, f"alert-{call_id}.ulaw")


def alert_contexts() -> list:
    """The answered side of an alert call (pbx.py adds these to the
    dialplan); the extension is the call's id."""
    play = os.path.join(ALERT_DIR, "alert-${EXTEN}")
    return ["; ---- Alert calls (alerts.py): only started by the service, never dialled ----",
            f"[{SAY_CONTEXT}]",
            "exten => _X!,1,Wait(1)",  # _X. would need 2+ digits: the first calls are 1, 2, 3
            f" same => n,Playback({play})",
            " same => n,Wait(1)",
            f" same => n,Playback({play})",
            " same => n,Playback(vm-goodbye)",
            " same => n,Hangup()",
            f"[{ASK_CONTEXT}]",
            "exten => _X!,1,Wait(1)",  # _X. would need 2+ digits: the first calls are 1, 2, 3
            " same => n,Set(TRIES=0)",
            f" same => n(ask),Read(KEY,{play},1,,1,6)",
            ' same => n,GotoIf($["${KEY}" = "1"]?yes)',
            " same => n,Set(TRIES=$[${TRIES} + 1])",
            " same => n,GotoIf($[${TRIES} < 3]?ask)",
            " same => n,Playback(vm-goodbye)",
            " same => n,Hangup()",
            " same => n(yes),Set(CURLOPT(conntimeout)=1)",
            " same => n,Set(CURLOPT(httptimeout)=3)",
            " same => n,Set(CURLOPT(httpheader)=X-LCR-Token: ${LCR_TOKEN})",
            f" same => n,Set(SAVED=${{CURL({PRESSED_URL}?id=${{EXTEN}})}})",
            " same => n,Playback(auth-thankyou)",
            " same => n,Hangup()"]


# -- speech ---------------------------------------------------------------------------------------

def speak(text: str) -> bytes:
    """The text as a WAV file, from Piper (run as `python -m piper`, from
    this service's own venv)."""
    if not TTS_MODEL or not os.path.exists(TTS_MODEL):
        raise HTTPException(503, "Text to speech isn't set up (PBX_TTS_MODEL).")
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "speech.wav")
        try:
            subprocess.run([sys.executable, "-m", "piper", "-m", TTS_MODEL, "-f", out], input=text.encode(),
                           capture_output=True, timeout=60, check=True)
            with open(out, "rb") as f:
                return f.read()
        except (subprocess.SubprocessError, OSError) as e:
            changes_log.info(f"Alert call: text to speech failed ({type(e).__name__})")
            raise HTTPException(502, "Text to speech failed.")


def _ulaw_byte(s: int) -> int:
    sign = 0x80 if s < 0 else 0
    s = min(-s if s < 0 else s, 32635) + 0x84
    exponent, mask = 7, 0x4000
    while exponent > 0 and not s & mask:
        exponent, mask = exponent - 1, mask >> 1
    return ~(sign | (exponent << 4) | ((s >> (exponent + 3)) & 0x0F)) & 0xFF


_ULAW = bytes(_ulaw_byte(s) for s in range(-32768, 32768))


def wav_to_ulaw(data: bytes) -> bytes:
    """16-bit WAV at any rate to 8 kHz mu-law with no header (what the IVR
    greetings use), in plain Python: no sox, no audioop."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise HTTPException(502, "Text to speech gave audio this can't read.")
        channels, rate = w.getnchannels(), w.getframerate()
        samples = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = samples[::channels]
    if rate > 8000:  # smooth first, so what's above 4 kHz doesn't fold back in as noise
        k = max(1, round(rate / 8000))
        acc, smooth = 0, []
        for i, v in enumerate(samples):
            acc += v - (samples[i - k] if i >= k else 0)
            smooth.append(acc // min(i + 1, k))
        samples = smooth
    n = len(samples)
    out = bytearray()
    step = rate / 8000
    for i in range(int(n / step)):
        pos = i * step
        j = int(pos)
        frac = pos - j
        nxt = samples[j + 1] if j + 1 < n else samples[j]
        out.append(_ULAW[int(samples[j] + (nxt - samples[j]) * frac) + 32768])
    return bytes(out)


def clean_text(text: str) -> str:
    text = re.sub(r"[^\w\s.,'!?:;-]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:TEXT_MAX]


# -- the calls --------------------------------------------------------------------------------------

def _local_only(request: Request):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1") or request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for"):
        raise HTTPException(403)


def _service(request: Request):
    _local_only(request)
    if not ALERT_TOKEN:
        raise HTTPException(503, "Alert calls are off (PBX_ALERT_TOKEN isn't set).")
    if not hmac.compare_digest(request.headers.get("x-alert-token", "").encode(), ALERT_TOKEN.encode()):
        raise HTTPException(403)


def _tidy(conn, now: float):
    for r in conn.execute("SELECT id FROM alert_calls WHERE created_at < ?", (now - KEEP_SECONDS,)).fetchall():
        try:
            os.remove(alert_path(r["id"]))
        except FileNotFoundError:
            pass
    conn.execute("DELETE FROM alert_calls WHERE created_at < ?", (now - KEEP_SECONDS,))


class AlertCall(BaseModel):
    number: str
    text: str
    kind: str = "say"


@public_router.post("/call")
def api_alert_call(req: AlertCall, request: Request):
    _service(request)
    if req.kind not in ("say", "ask"):
        raise HTTPException(400, "kind is say or ask.")
    kind, national, _ = classify_dialled(req.number)
    if kind != "mobile":
        raise HTTPException(400, "Alert calls go to Irish mobile numbers only.")
    text = clean_text(req.text)
    if len(text) < 3:
        raise HTTPException(400, "Nothing to say.")
    now = time.time()
    with get_db() as conn:
        conn.execute(SCHEMA)
        _tidy(conn, now)
        tid = MAIN_TENANT
        if ALERT_TENANT:
            r = conn.execute("SELECT id FROM tenants WHERE code = ? AND status = 'active'", (ALERT_TENANT,)).fetchone()
            if not r:
                raise HTTPException(503, f"Alert calls are off (PBX_ALERT_TENANT {ALERT_TENANT} isn't an active tenant).")
            tid = r["id"]
        if not re.fullmatch(r"\d{3,4}", ALERT_EXT) or not conn.execute(
                "SELECT 1 FROM extensions WHERE ext = ? AND enabled = 1 AND tenant_id = ?",
                (ALERT_EXT, tid)).fetchone():
            raise HTTPException(503, "Alert calls are off (PBX_ALERT_EXT isn't an extension of that tenant that's on).")
        dial = tenant_names(conn, tid).c2c_dial
        hour = now - 3600
        if conn.execute("SELECT COUNT(*) FROM alert_calls WHERE number = ? AND created_at > ?",
                        (national, hour)).fetchone()[0] >= PER_NUMBER_HOUR or \
                conn.execute("SELECT COUNT(*) FROM alert_calls WHERE created_at > ?", (hour,)).fetchone()[0] >= PER_HOUR:
            raise HTTPException(429, "Too many alert calls this hour.")
        audio = wav_to_ulaw(speak(text))
        call_id = conn.execute("INSERT INTO alert_calls (kind, number, created_at) VALUES (?, ?, ?)",
                               (req.kind, national, now)).lastrowid
        conn.commit()
    from ivr import write_ulaw
    write_ulaw(alert_path(call_id), audio)
    leg = f"{ALERT_EXT}{national}"  # digits only (checked above)
    context = ASK_CONTEXT if req.kind == "ask" else SAY_CONTEXT
    out = asterisk_cli(f"channel originate Local/{leg}@{dial}/n extension {call_id}@{context}")
    changes_log.info(f"Alert call {call_id} ({req.kind}) to {national[:-4]}xxxx from extension {ALERT_EXT}"
                     f" of tenant {ALERT_TENANT or 'main'}")
    if out is None or re.search(r"no such command|usage:|unable to", out, re.I):
        raise HTTPException(502, "Asterisk didn't start the call.")
    return {"id": call_id}


@public_router.get("/call/{call_id}")
def api_alert_status(call_id: int, request: Request):
    _service(request)
    with get_db() as conn:
        conn.execute(SCHEMA)
        r = conn.execute("SELECT * FROM alert_calls WHERE id = ?", (call_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return {"id": r["id"], "pressed": bool(r["pressed_at"]), "age": round(time.time() - r["created_at"])}


@public_router.get("/pressed", response_class=PlainTextResponse)
def api_alert_pressed(id: int, request: Request):
    """Asterisk, when the person answering an ask call presses 1."""
    _local_only(request)
    with get_db() as conn:
        if not hmac.compare_digest(request.headers.get("x-lcr-token", "").encode(), _lcr_token(conn).encode()):
            raise HTTPException(403)
        conn.execute(SCHEMA)
        conn.execute("UPDATE alert_calls SET pressed_at = ? WHERE id = ? AND kind = 'ask' AND pressed_at IS NULL",
                     (time.time(), id))
        conn.commit()
    changes_log.info(f"Alert call {id}: 1 pressed")
    return PlainTextResponse("OK")
