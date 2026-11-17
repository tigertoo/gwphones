"""Desk phones (Yealink): a tenant's admins add their own phones, move
them between extensions, and see what to enter on the phone. Admins only,
on a router that checks it itself, as in routing.py.

gwphones keeps it to the tenant's own phones and one phone per extension;
a MAC another tenant has is refused without saying so. The phone's
provisioning and web-page passwords are shown to the admin (they're for
the tenant's own phones) but never written to the audit log. Removing a
phone can also give its extension a new SIP password, since a phone
keeps its settings until it's factory-reset.
"""
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import auth, gw
from .app import Signed, app
from .phone import _audit, _done, _saving
from .routing import admin

router = APIRouter(prefix="/api", dependencies=[Depends(admin)])

PHONE_KEYS = ("mac", "mac_pretty", "ext", "model", "username", "password", "admin_password", "created_at",
              "last_fetch_at", "last_fetch_agent", "firmware", "upgrade_to", "upgrade_stuck", "rps_status", "rps_detail")


def _mac(value: str) -> str:
    """12 hex digits, separators dropped: the only thing that goes into a
    gwphones path."""
    mac = re.sub(r"[\s:.-]", "", value or "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", mac):
        raise auth.Refused("That doesn't look like a MAC address: 12 letters and digits, on the sticker under the phone.", 400)
    return mac


def _phones(me: Signed) -> dict:
    return gw.call(me.user, "GET", "/desk-phones")


class NewPhone(BaseModel):
    mac: str
    ext: str
    model: str = ""


class PhoneChange(BaseModel):
    ext: str
    model: str = ""


def _phone(p: dict) -> dict:
    out = {k: p.get(k) for k in PHONE_KEYS}
    if out["rps_status"] == "failed":  # its detail can name our settings: a plain message instead
        out["rps_detail"] = "it couldn't be registered just now. Try again later."
    return out


@router.get("/desk-phones")
def api_desk_phones(me: Signed = Depends(admin)):
    r = _phones(me)
    return {"phones": [_phone(p) for p in r["phones"]], "models": r["models"],
            "server_url": r["server_url"], "ready": r["ready"], "repeat_minutes": r["repeat_minutes"],
            "rps": bool(r.get("rps"))}


@router.post("/desk-phones")
def api_new_desk_phone(req: NewPhone, request: Request, me: Signed = Depends(admin)):
    mac = _mac(req.mac)
    _saving(me.user)
    r = gw.call(me.user, "POST", "/desk-phones", json={"mac": mac, "ext": req.ext.strip(), "model": req.model})
    _audit(me, request, "desk phone added", f"{mac} on {req.ext.strip()}")
    data = r.data if isinstance(r, gw.Pending) else r
    return _done(r, {"rps": (data.get("rps") or {}).get("status", "")})


@router.put("/desk-phones/{mac}")
def api_change_desk_phone(mac: str, req: PhoneChange, request: Request, me: Signed = Depends(admin)):
    mac = _mac(mac)
    _saving(me.user)
    r = gw.call(me.user, "PUT", f"/desk-phones/{mac}", json={"ext": req.ext.strip(), "model": req.model})
    _audit(me, request, "desk phone changed", f"{mac} on {req.ext.strip()}")
    return _done(r)


@router.post("/desk-phones/{mac}/new-password")
def api_desk_phone_new_password(mac: str, request: Request, me: Signed = Depends(admin)):
    mac = _mac(mac)
    _saving(me.user)
    r = gw.call(me.user, "POST", f"/desk-phones/{mac}/new-password")
    _audit(me, request, "desk phone: new provisioning password", mac)
    return _done(r)


@router.post("/desk-phones/{mac}/rps")
def api_desk_phone_rps(mac: str, request: Request, me: Signed = Depends(admin)):
    mac = _mac(mac)
    _saving(me.user)
    r = gw.call(me.user, "POST", f"/desk-phones/{mac}/rps")
    _audit(me, request, "desk phone: Yealink RPS sync", mac)
    return {"ok": True, "registered": (r.get("rps") or {}).get("status") == "registered"}


@router.delete("/desk-phones/{mac}")
def api_remove_desk_phone(mac: str, request: Request, new_sip_password: bool = False, me: Signed = Depends(admin)):
    mac = _mac(mac)
    phone = next((p for p in _phones(me)["phones"] if p["mac"] == mac), None)
    if not phone:
        raise auth.Refused("Not found.", 404)
    _saving(me.user)
    # The new SIP password first: if that fails, the phone is still listed
    # and Remove can simply be tried again.
    if new_sip_password:
        gw.call(me.user, "POST", f"/extensions/{phone['ext']}/reset-password")
        _audit(me, request, "password reset (desk phone removed)", phone["ext"])
    r = gw.call(me.user, "DELETE", f"/desk-phones/{mac}")
    _audit(me, request, "desk phone removed", mac)
    return _done(r)


@router.get("/desk-phones/{mac}/config")
def api_desk_phone_config(mac: str, me: Signed = Depends(admin)):
    """What the phone gets, with its passwords hidden by gwphones."""
    mac = _mac(mac)
    r = gw.call(me.user, "GET", f"/desk-phones/{mac}/config.cfg", raw=True)
    return PlainTextResponse(r.text, headers={"Cache-Control": "no-store"})


app.include_router(router)
