"""
IVR menus: "press 1 for sales, 2 for support". Each menu has a number
(5XX) that phones, inbound routes and call links can ring, a greeting, and
a target per key; pbx.py writes them into the dialplan.

Greetings arrive already converted by the dashboard page (an uploaded file
or a recording from the browser's microphone, as 8 kHz mu-law with no
header), so the server never reads an audio file it was sent: it only
checks the length and stores the bytes for Asterisk to play.
"""

import asyncio
import json
import os
from typing import Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from pbx import (IVR_DIR, MENU_KEYS, MENU_MAX, MENU_MIN, _clean_name, _in_range, _lock, _used_as_target,
                 apply_config, check_target, get_db, greeting_path)

router = APIRouter(prefix="/api/pbx")

GREETING_MIN_BYTES = 4000  # half a second at 8000 bytes a second
GREETING_MAX_SECONDS = 120
GREETING_MAX_BYTES = GREETING_MAX_SECONDS * 8000


class MenuRequest(BaseModel):
    number: str = ""  # next free number if blank (create only)
    name: str
    options: Dict[str, str] = {}
    fallback: str = ""  # where the call goes after the last try; blank = goodbye
    timeout: int = 6
    retries: int = 3
    direct_dial: bool = False


def _target(conn, target: str, number: str) -> str:
    """Any existing extension: a switched-off one is skipped (the key
    counts as wrong) until it's back on. A menu may point at itself."""
    return check_target(conn, target, extras=True, itself=number)


def _clean(conn, req: MenuRequest, number: str) -> dict:
    options = {}
    for key, target in req.options.items():
        if key not in MENU_KEYS or len(key) != 1:
            raise HTTPException(400, f"Keys are 0-9, * and #, not {key!r}.")
        if target.strip():
            options[key] = _target(conn, target, number)
    if not options:
        raise HTTPException(400, "Give at least one key somewhere to go.")
    if not 2 <= req.timeout <= 30:
        raise HTTPException(400, "The wait for a key must be 2-30 seconds.")
    if not 1 <= req.retries <= 5:
        raise HTTPException(400, "The greeting can be played 1-5 times.")
    fallback = _target(conn, req.fallback, number) if req.fallback.strip() else ""
    return {"name": _clean_name(req.name), "options": options, "fallback": fallback,
            "timeout": req.timeout, "retries": req.retries, "direct_dial": int(req.direct_dial)}


def _summary(m: dict) -> str:
    keys = ", ".join(f"{k}->{v}" for k, v in sorted(m["options"].items()))
    return f"{keys}; fallback {m['fallback'] or 'goodbye'}"


@router.post("/ivr")
def api_create_menu(req: MenuRequest):
    with _lock, get_db() as conn:
        taken = {r["number"] for r in conn.execute("SELECT number FROM ivr_menus")}
        if req.number.strip():
            number = _in_range(req.number, MENU_MIN, MENU_MAX, "Menu number")
            if number in taken:
                raise HTTPException(409, f"Menu {number} already exists.")
        else:
            free = [str(n) for n in range(MENU_MIN, MENU_MAX + 1) if str(n) not in taken]
            if not free:
                raise HTTPException(409, "No free menu numbers left.")
            number = free[0]
        m = _clean(conn, req, number)
        conn.execute("INSERT INTO ivr_menus (number, name, options, fallback, timeout, retries, direct_dial) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (number, m["name"], json.dumps(m["options"]), m["fallback"], m["timeout"], m["retries"],
                      m["direct_dial"]))
        conn.commit()
        result = apply_config(conn, f"Created IVR menu {number} ({m['name']}): {_summary(m)}")
    return {"number": number, **result}


@router.put("/ivr/{number}")
def api_update_menu(number: str, req: MenuRequest):
    with _lock, get_db() as conn:
        m = _clean(conn, req, number)
        if conn.execute("UPDATE ivr_menus SET name = ?, options = ?, fallback = ?, timeout = ?, retries = ?, "
                        "direct_dial = ? WHERE number = ?",
                        (m["name"], json.dumps(m["options"]), m["fallback"], m["timeout"], m["retries"],
                         m["direct_dial"], number)).rowcount == 0:
            raise HTTPException(404, "Menu not found.")
        conn.commit()
        return apply_config(conn, f"Updated IVR menu {number}: {_summary(m)}")


@router.delete("/ivr/{number}")
def api_delete_menu(number: str):
    with _lock, get_db() as conn:
        used = _used_as_target(conn, number)
        if used:
            raise HTTPException(409, f"Menu {number} is {used}. Change that first.")
        if conn.execute("DELETE FROM ivr_menus WHERE number = ?", (number,)).rowcount == 0:
            raise HTTPException(404, "Menu not found.")
        conn.commit()
        try:
            os.remove(greeting_path(number))
        except FileNotFoundError:
            pass
        return apply_config(conn, f"Deleted IVR menu {number}")


def _menu_or_404(conn, number: str):
    if not conn.execute("SELECT 1 FROM ivr_menus WHERE number = ?", (number,)).fetchone():
        raise HTTPException(404, "Menu not found.")


def _store_greeting(number: str, audio: bytes) -> dict:
    with _lock, get_db() as conn:
        _menu_or_404(conn, number)
        os.makedirs(IVR_DIR, exist_ok=True)
        path = greeting_path(number)
        # Next to the file, not in /tmp: os.replace can't cross filesystems.
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(audio)
        os.chmod(tmp, 0o640)  # the folder's setgid bit gives it to the asterisk group
        os.replace(tmp, path)
        return apply_config(conn, f"New greeting for IVR menu {number} ({len(audio) // 8000}s)")


@router.put("/ivr/{number}/greeting")
async def api_set_greeting(number: str, request: Request):
    """The body is the greeting as 8 kHz mu-law (see the module docstring).
    Saving runs in a thread: the lock and the reload would otherwise hold
    up every other request, /lcr/route (mid-call) included."""
    if int(request.headers.get("content-length") or 0) > GREETING_MAX_BYTES:
        raise HTTPException(400, f"Greetings can be up to {GREETING_MAX_SECONDS // 60} minutes long.")
    audio = await request.body()
    if len(audio) > GREETING_MAX_BYTES:
        raise HTTPException(400, f"Greetings can be up to {GREETING_MAX_SECONDS // 60} minutes long.")
    if len(audio) < GREETING_MIN_BYTES:
        raise HTTPException(400, "That greeting is too short.")
    return await asyncio.to_thread(_store_greeting, number, audio)


@router.get("/ivr/{number}/greeting")
def api_get_greeting(number: str):
    with get_db() as conn:
        _menu_or_404(conn, number)
    try:
        with open(greeting_path(number), "rb") as f:
            audio = f.read()
    except FileNotFoundError:
        raise HTTPException(404, "This menu has no greeting yet.")
    return Response(audio, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@router.delete("/ivr/{number}/greeting")
def api_delete_greeting(number: str):
    with _lock, get_db() as conn:
        _menu_or_404(conn, number)
        try:
            os.remove(greeting_path(number))
        except FileNotFoundError:
            pass
        return apply_config(conn, f"Removed the greeting of IVR menu {number}")
