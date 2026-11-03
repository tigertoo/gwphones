"""
Call recording. Asterisk records (MixMonitor, set up by [portal-rec] in
the generated dialplan, or the Record button here for a call in progress)
into REC_DIR as 8 kHz mu-law; this lists them, plays and downloads them
as WAV, deletes them, and deletes them by itself after the number of days
in the settings. Also: which extensions record everything, and the notice
outside callers hear ("calls may be recorded").
"""

import asyncio
import os
import re
import struct
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ivr import read_ulaw, write_ulaw
from pbx import (PBX_TIMEZONE, REC_DIR, REC_NAME_RE, REC_NOTICE, _lock, _set_setting, apply_config, asterisk_cli, channel_ext,
                 changes_log, current_tenant, get_db, get_settings, get_status)

router = APIRouter(prefix="/api/pbx")

MAX_LISTED = 500


def _mulaw_tables():
    """mu-law byte -> low and high byte of the 16-bit sample, as tables for
    bytes.translate: fast, and no per-sample Python objects (an hour's
    call is 29 million samples)."""
    lo, hi = bytearray(256), bytearray(256)
    for u in range(256):
        v = ~u & 0xFF
        sign, exponent, mantissa = v & 0x80, (v >> 4) & 0x07, v & 0x0F
        s = (((mantissa << 3) + 0x84) << exponent) - 0x84
        lo[u], hi[u] = struct.pack("<h", -s if sign else s)
    return bytes(lo), bytes(hi)


MULAW_LO, MULAW_HI = _mulaw_tables()


def _wav(ulaw: bytes) -> bytes:
    """mu-law to 16-bit PCM WAV, which every browser and player opens."""
    out = bytearray(44 + 2 * len(ulaw))
    out[0:44] = (b"RIFF" + struct.pack("<I", 36 + 2 * len(ulaw)) + b"WAVEfmt " +
                 struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data" + struct.pack("<I", 2 * len(ulaw)))
    out[44::2] = ulaw.translate(MULAW_LO)
    out[45::2] = ulaw.translate(MULAW_HI)
    return bytes(out)


def _ready() -> bool:
    return os.path.isdir(REC_DIR) and os.access(REC_DIR, os.W_OK | os.X_OK)


@router.get("/recordings")
def api_recordings(ext: str = "", q: str = ""):
    q = re.sub(r"\D", "", q)
    found = []
    try:
        names = os.listdir(REC_DIR)
    except OSError:
        names = []
    for name in names:
        m = REC_NAME_RE.fullmatch(name)
        if not m or (ext and m.group(2) != ext) or (q and q not in m.group(3)):
            continue
        try:
            size = os.path.getsize(os.path.join(REC_DIR, name))
        except OSError:
            continue
        found.append({"name": name, "started": m.group(1), "ext": m.group(2), "other": m.group(3),
                      "direction": m.group(4), "seconds": size // 8000})
    found.sort(key=lambda r: r["started"], reverse=True)
    with get_db() as conn:
        days = int(get_settings(conn)["rec_days"])
    return {"recordings": found[:MAX_LISTED], "total": len(found), "ready": _ready(), "dir": REC_DIR,
            "days": days, "notice": os.path.exists(REC_NOTICE), "timezone": PBX_TIMEZONE}


def _path(name: str) -> str:
    if not REC_NAME_RE.fullmatch(name):
        raise HTTPException(404, "Recording not found.")
    return os.path.join(REC_DIR, name)


@router.get("/recordings/file/{name}")
def api_recording_file(name: str, download: bool = False):
    try:
        with open(_path(name), "rb") as f:
            audio = f.read()
    except FileNotFoundError:
        raise HTTPException(404, "Recording not found.")
    filename = name[:-len(".ulaw")] + ".wav"
    return Response(_wav(audio), media_type="audio/wav",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"'})


@router.delete("/recordings/file/{name}")
def api_delete_recording(name: str):
    try:
        os.remove(_path(name))
    except FileNotFoundError:
        raise HTTPException(404, "Recording not found.")
    changes_log.info(f"Deleted recording {name}")
    return {"ok": True}


class RecordingSettings(BaseModel):
    days: int


@router.put("/recordings/settings")
def api_recording_settings(req: RecordingSettings):
    if not 1 <= req.days <= 3650:
        raise HTTPException(400, "Keep recordings for 1-3650 days.")
    with _lock, get_db() as conn:
        _set_setting(conn, "rec_days", str(req.days))
        conn.commit()
    changes_log.info(f"Recordings are kept for {req.days} days")
    expire_recordings()
    return {"ok": True}


class ExtensionRecording(BaseModel):
    always: bool


@router.put("/extensions/{ext}/recording")
def api_extension_recording(ext: str, req: ExtensionRecording):
    with _lock, get_db() as conn:
        if conn.execute("UPDATE extensions SET record = ? WHERE ext = ? AND tenant_id = ?",
                        (int(req.always), ext, current_tenant())).rowcount == 0:
            raise HTTPException(404, "Extension not found.")
        conn.commit()
        return apply_config(conn, f"Extension {ext}: record every call={req.always}")


# ---- The notice outside callers hear while any extension records everything

def _store_notice(audio: bytes) -> dict:
    with _lock, get_db() as conn:
        write_ulaw(REC_NOTICE, audio)
        return apply_config(conn, f"New recording notice ({len(audio) // 8000}s)")


@router.put("/recordings/notice")
async def api_set_notice(request: Request):
    audio = await read_ulaw(request)
    return await asyncio.to_thread(_store_notice, audio)


@router.get("/recordings/notice")
def api_get_notice():
    try:
        with open(REC_NOTICE, "rb") as f:
            return Response(f.read(), media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        raise HTTPException(404, "There's no notice yet.")


@router.delete("/recordings/notice")
def api_delete_notice():
    with _lock, get_db() as conn:
        try:
            os.remove(REC_NOTICE)
        except FileNotFoundError:
            pass
        return apply_config(conn, "Removed the recording notice")


# ---- The Record button: record a call in progress from now on

class StartRecording(BaseModel):
    channel: str


def _channel_field(info: str, label: str) -> str:
    m = re.search(rf"^\s*{label}:\s*(.*?)\s*$", info, re.M)
    return m.group(1) if m else ""


@router.post("/recordings/start")
def api_start_recording(req: StartRecording):
    """Only an extension's channel in a call that's up, as listed right now
    (so the name passed to the CLI is one Asterisk gave us)."""
    ext = channel_ext(req.channel)
    if not ext or not re.fullmatch(r"PJSIP/[0-9]{3,7}-[0-9a-f]{8}", req.channel):
        raise HTTPException(400, "Only an extension's call can be recorded.")
    status = get_status()
    chan = next((c for c in status["channels"] if c["channel"] == req.channel), None)
    if not chan or not chan["recordable"]:
        raise HTTPException(409, "That call has ended or isn't connected yet.")
    if chan["recording"]:
        raise HTTPException(409, "That call is already being recorded.")
    info = asterisk_cli(f"core show channel {req.channel}") or ""
    uniqueid = _channel_field(info, "UniqueID")
    other = re.sub(r"\D", "", _channel_field(info, "Connected Line ID"))[:20] or "unknown"
    if not re.fullmatch(r"[0-9.]{1,40}", uniqueid):
        raise HTTPException(409, "Couldn't read that call from Asterisk. Has it just ended?")
    started = datetime.now(timezone.utc).astimezone(ZoneInfo(PBX_TIMEZONE)).strftime("%Y%m%d-%H%M%S")
    name = f"{started}_{ext}_{other}_req_{uniqueid}.ulaw"
    out = asterisk_cli(f"mixmonitor start {req.channel} {os.path.join(REC_DIR, name)},b")
    if out is None or "No such" in out or "rror" in out:
        raise HTTPException(500, f"Asterisk didn't start recording: {(out or 'not reachable').strip()[:200]}")
    changes_log.info(f"Started recording {req.channel} on request: {name}")
    return {"ok": True, "name": name}


# ---- Keeping only what the settings say

def expire_recordings(now: Optional[float] = None) -> int:
    with get_db() as conn:
        days = int(get_settings(conn)["rec_days"])
    cutoff = (now or time.time()) - days * 86400
    removed = 0  # a call still being recorded was written to just now, so it's never old enough
    try:
        names = os.listdir(REC_DIR)
    except OSError:
        return 0
    for name in names:
        if not REC_NAME_RE.fullmatch(name):
            continue
        path = os.path.join(REC_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        changes_log.info(f"Deleted {removed} recording(s) older than {days} days")
    return removed


async def recordings_expiry_loop():
    while True:
        try:
            await asyncio.to_thread(expire_recordings)
        except Exception:
            pass
        await asyncio.sleep(3600)
