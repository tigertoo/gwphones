"""
Business hours: a schedule (4XX) that sends calls to one place when open
and another when closed, by the weekly hours, holiday dates and an
override ("closed today"). Inbound numbers, call links, IVR keys and phones
can ring a schedule like any other number; pbx.py writes them into the
dialplan.
"""

import json
import re
from datetime import date, datetime, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pbx import (DAYS, HOURS_MAX, HOURS_MIN, PBX_TIMEZONE, _clean_name, _in_range, _lock, _used_as_target,
                 apply_config, check_target, get_db)

router = APIRouter(prefix="/api/pbx")

TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
MAX_RANGES = 4  # per day: e.g. morning and afternoon, around lunch
MAX_HOLIDAYS = 60
OVERRIDES = ("", "open", "closed")


class HoursRequest(BaseModel):
    number: str = ""  # next free number if blank (create only)
    name: str
    week: Dict[str, List[List[str]]] = {}
    holidays: List[str] = []
    open_target: str
    closed_target: str
    override: str = ""


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(PBX_TIMEZONE)).date()


def _clean(conn, req: HoursRequest) -> dict:
    week = {}
    for day in DAYS:
        ranges = []
        for r in req.week.get(day, []):
            if len(r) != 2 or not all(TIME_RE.fullmatch(t or "") for t in r):
                raise HTTPException(400, f"Times are like 09:00 or 17:30 ({day}).")
            if r[0] >= r[1]:
                raise HTTPException(400, f"Each opening must end after it starts, on the same day ({day} {r[0]}-{r[1]}).")
            ranges.append([r[0], r[1]])
        ranges.sort()
        if len(ranges) > MAX_RANGES:
            raise HTTPException(400, f"At most {MAX_RANGES} openings a day ({day}).")
        if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
            raise HTTPException(400, f"The openings on {day} overlap.")
        week[day] = ranges
    holidays = set()
    for h in req.holidays:
        try:
            d = date.fromisoformat(h)
        except ValueError:
            raise HTTPException(400, f"{h!r} isn't a date (YYYY-MM-DD).")
        if d >= _today():  # past ones are dropped
            holidays.add(d.isoformat())
    if len(holidays) > MAX_HOLIDAYS:
        raise HTTPException(400, f"At most {MAX_HOLIDAYS} closed days.")
    if req.override not in OVERRIDES:
        raise HTTPException(400, "The override is open, closed or none.")
    return {"name": _clean_name(req.name), "week": week, "holidays": sorted(holidays),
            "open_target": check_target(conn, req.open_target, extras=True, schedules=False),
            "closed_target": check_target(conn, req.closed_target, extras=True, schedules=False),
            "override": req.override}


def _summary(h: dict) -> str:
    days = ", ".join(f"{d} " + " ".join(f"{a}-{b}" for a, b in h["week"][d]) for d in DAYS if h["week"][d])
    return (f"open {days or 'never'}; closed on {', '.join(h['holidays']) or 'no extra days'}; "
            f"open -> {h['open_target']}, closed -> {h['closed_target']}, override={h['override'] or 'none'}")


@router.post("/hours")
def api_create_hours(req: HoursRequest):
    with _lock, get_db() as conn:
        taken = {r["number"] for r in conn.execute("SELECT number FROM business_hours")}
        if req.number.strip():
            number = _in_range(req.number, HOURS_MIN, HOURS_MAX, "Schedule number")
            if number in taken:
                raise HTTPException(409, f"Schedule {number} already exists.")
        else:
            free = [str(n) for n in range(HOURS_MIN, HOURS_MAX + 1) if str(n) not in taken]
            if not free:
                raise HTTPException(409, "No free schedule numbers left.")
            number = free[0]
        h = _clean(conn, req)
        conn.execute("INSERT INTO business_hours (number, name, week, holidays, open_target, closed_target, override) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (number, h["name"], json.dumps(h["week"]), json.dumps(h["holidays"]), h["open_target"],
                      h["closed_target"], h["override"]))
        conn.commit()
        result = apply_config(conn, f"Created business hours {number} ({h['name']}): {_summary(h)}")
    return {"number": number, **result}


@router.put("/hours/{number}")
def api_update_hours(number: str, req: HoursRequest):
    with _lock, get_db() as conn:
        h = _clean(conn, req)
        if conn.execute("UPDATE business_hours SET name = ?, week = ?, holidays = ?, open_target = ?, "
                        "closed_target = ?, override = ? WHERE number = ?",
                        (h["name"], json.dumps(h["week"]), json.dumps(h["holidays"]), h["open_target"],
                         h["closed_target"], h["override"], number)).rowcount == 0:
            raise HTTPException(404, "Schedule not found.")
        conn.commit()
        return apply_config(conn, f"Updated business hours {number}: {_summary(h)}")


class OverrideRequest(BaseModel):
    override: str


@router.put("/hours/{number}/override")
def api_hours_override(number: str, req: OverrideRequest):
    """Open or closed whatever the clock says, or back to the hours."""
    if req.override not in OVERRIDES:
        raise HTTPException(400, "The override is open, closed or none.")
    with _lock, get_db() as conn:
        if conn.execute("UPDATE business_hours SET override = ? WHERE number = ?",
                        (req.override, number)).rowcount == 0:
            raise HTTPException(404, "Schedule not found.")
        conn.commit()
        return apply_config(conn, f"Business hours {number}: override={req.override or 'none (by the hours)'}")


@router.delete("/hours/{number}")
def api_delete_hours(number: str):
    with _lock, get_db() as conn:
        used = _used_as_target(conn, number)
        if used:
            raise HTTPException(409, f"Schedule {number} is {used}. Change that first.")
        if conn.execute("DELETE FROM business_hours WHERE number = ?", (number,)).rowcount == 0:
            raise HTTPException(404, "Schedule not found.")
        conn.commit()
        return apply_config(conn, f"Deleted business hours {number}")
