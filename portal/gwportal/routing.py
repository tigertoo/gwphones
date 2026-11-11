"""Ring groups, IVR menus (with their greetings) and business hours: for a
tenant's admins only, so every route here sits on a router that checks
that itself (a route added here can't forget it).

Destinations come from gwphones as a labelled list, so these pages never
need to know how one is written. Greetings travel as the 8 kHz mu-law the
page converts them to, at most GREETING_MAX_BYTES, checked before reading.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from . import auth, gw
from .app import Signed, app, signed_in
from .phone import _audit, _done, _saving

GREETING_MAX_BYTES = 120 * 8000  # two minutes, as gwphones allows
GREETING_MIN_BYTES = 4000


def admin(me: Signed = Depends(signed_in())) -> Signed:
    if me.user["role"] != "admin":
        raise auth.Refused("Only your organisation's admins can do that.", 403)
    return me


router = APIRouter(prefix="/api", dependencies=[Depends(admin)])


@router.get("/routing")
def api_routing(me: Signed = Depends(admin)):
    r = gw.call(me.user, "GET", "/routing")
    pick = lambda d, keys: {k: d.get(k) for k in keys}
    return {
        "ring_groups": [pick(g, ("number", "name", "strategy", "ring_time", "members", "voicemail")) for g in r["ring_groups"]],
        "ivr_menus": [pick(m, ("number", "name", "options", "fallback", "timeout", "retries", "direct_dial", "greeting"))
                      for m in r["ivr_menus"]],
        "business_hours": [pick(h, ("number", "name", "week", "holidays", "open_target", "closed_target", "override",
                                    "open_now")) for h in r["business_hours"]],
        "destinations": [pick(d, ("value", "label", "kind", "enabled")) for d in r["destinations"]],
        "numbers": [pick(n, ("did", "label", "target")) for n in r["numbers"]],
        "limits": pick(r["limits"], ("group_min", "group_max", "menu_min", "menu_max", "hours_min", "hours_max")),
        "timezone": r["timezone"],
    }


def _save(me: Signed, request: Request, method: str, path: str, body: dict, what: str):
    _saving(me.user)
    r = gw.call(me.user, method, path, json=body)
    _audit(me, request, what, path)
    data = r.data if isinstance(r, gw.Pending) else r
    extra = {"number": data["number"]} if data.get("number") else None
    return _done(r, extra)


def _body(model: BaseModel) -> dict:
    return model.model_dump(exclude_none=True)


# -- ring groups ----------------------------------------------------------------------------

class Group(BaseModel):
    number: Optional[str] = None
    name: str
    strategy: str = "ringall"
    ring_time: int = 25
    members: List[str] = []
    voicemail: str = ""


@router.post("/groups")
def api_new_group(req: Group, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "POST", "/ringgroups", _body(req), "ring group created")


@router.put("/groups/{number}")
def api_change_group(number: str, req: Group, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "PUT", f"/ringgroups/{number}", _body(req), "ring group changed")


@router.delete("/groups/{number}")
def api_delete_group(number: str, request: Request, me: Signed = Depends(admin)):
    _saving(me.user)
    r = gw.call(me.user, "DELETE", f"/ringgroups/{number}")
    _audit(me, request, "ring group deleted", number)
    return _done(r)


# -- IVR menus and greetings ------------------------------------------------------------------

class Menu(BaseModel):
    number: Optional[str] = None
    name: str
    options: Dict[str, str] = {}
    fallback: str = ""
    timeout: int = 6
    retries: int = 3
    direct_dial: bool = False


@router.post("/menus")
def api_new_menu(req: Menu, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "POST", "/ivr", _body(req), "menu created")


@router.put("/menus/{number}")
def api_change_menu(number: str, req: Menu, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "PUT", f"/ivr/{number}", _body(req), "menu changed")


@router.delete("/menus/{number}")
def api_delete_menu(number: str, request: Request, me: Signed = Depends(admin)):
    _saving(me.user)
    r = gw.call(me.user, "DELETE", f"/ivr/{number}")
    _audit(me, request, "menu deleted", number)
    return _done(r)


@router.get("/menus/{number}/greeting")
def api_greeting(number: str, me: Signed = Depends(admin)):
    r = gw.call(me.user, "GET", f"/ivr/{number}/greeting", raw=True)
    return Response(r.content, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


@router.put("/menus/{number}/greeting")
async def api_set_greeting(number: str, request: Request, me: Signed = Depends(admin)):
    if int(request.headers.get("content-length") or 0) > GREETING_MAX_BYTES:
        raise auth.Refused("Greetings can be up to 2 minutes long.", 400)
    audio = await request.body()
    if len(audio) > GREETING_MAX_BYTES:
        raise auth.Refused("Greetings can be up to 2 minutes long.", 400)
    if len(audio) < GREETING_MIN_BYTES:
        raise auth.Refused("That greeting is too short.", 400)
    _saving(me.user)
    from starlette.concurrency import run_in_threadpool
    r = await run_in_threadpool(gw.call, me.user, "PUT", f"/ivr/{number}/greeting", None, None, False, audio)
    _audit(me, request, "menu greeting changed", f"{number} ({len(audio) // 8000}s)")
    return _done(r)


@router.delete("/menus/{number}/greeting")
def api_delete_greeting(number: str, request: Request, me: Signed = Depends(admin)):
    _saving(me.user)
    r = gw.call(me.user, "DELETE", f"/ivr/{number}/greeting")
    _audit(me, request, "menu greeting removed", number)
    return _done(r)


# -- business hours ---------------------------------------------------------------------------

class Hours(BaseModel):
    number: Optional[str] = None
    name: str
    week: Dict[str, List[List[str]]] = {}
    holidays: List[str] = []
    open_target: str = ""
    closed_target: str = ""
    override: str = ""


class Override(BaseModel):
    override: str = ""  # "", "open" or "closed"


@router.post("/hours")
def api_new_hours(req: Hours, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "POST", "/hours", _body(req), "business hours created")


@router.put("/hours/{number}")
def api_change_hours(number: str, req: Hours, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "PUT", f"/hours/{number}", _body(req), "business hours changed")


@router.put("/hours/{number}/override")
def api_override(number: str, req: Override, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "PUT", f"/hours/{number}/override", _body(req), f"business hours override {req.override or 'off'}")


@router.delete("/hours/{number}")
def api_delete_hours(number: str, request: Request, me: Signed = Depends(admin)):
    _saving(me.user)
    r = gw.call(me.user, "DELETE", f"/hours/{number}")
    _audit(me, request, "business hours deleted", number)
    return _done(r)


# -- where the organisation's numbers go ------------------------------------------------------

class NumberTarget(BaseModel):
    target: str


@router.put("/numbers/{did}")
def api_number_target(did: str, req: NumberTarget, request: Request, me: Signed = Depends(admin)):
    """Only where the number sends calls: the numbers themselves are ours."""
    return _save(me, request, "PUT", f"/inbound/{did}/target", _body(req), f"number {did} now goes to {req.target}")


# -- call links -------------------------------------------------------------------------------
# A URL anyone can open to call the organisation from a browser. Video is
# a paid add-on that isn't built yet: the portal keeps each link's video
# setting as the operator set it, and its own new links are audio only.

LINK_KEYS = ("id", "label", "target", "video", "max_minutes", "enabled", "live", "last_opened_at", "url", "qr")


class CallLink(BaseModel):
    label: str
    target: str
    max_minutes: int = 30
    enabled: bool = True


def _links(me: Signed) -> List[dict]:
    return gw.call(me.user, "GET", "/call-links")["links"]


@router.get("/call-links")
def api_call_links(me: Signed = Depends(admin)):
    r = gw.call(me.user, "GET", "/call-links")
    return {"links": [{k: l.get(k) for k in LINK_KEYS} for l in r["links"]], "ready": r["ready"],
            "max_minutes": r["max_minutes"]}


@router.post("/call-links")
def api_new_call_link(req: CallLink, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "POST", "/call-links", {**_body(req), "video": False}, f"call link created ({req.label})")


@router.put("/call-links/{link_id}")
def api_change_call_link(link_id: int, req: CallLink, request: Request, me: Signed = Depends(admin)):
    current = next((l for l in _links(me) if l["id"] == link_id), None)
    if not current:
        raise auth.Refused("Not found.", 404)
    body = {**_body(req), "video": current["video"]}  # as the operator set it
    return _save(me, request, "PUT", f"/call-links/{link_id}", body, f"call link {link_id} changed")


@router.post("/call-links/{link_id}/new-url")
def api_call_link_new_url(link_id: int, request: Request, me: Signed = Depends(admin)):
    return _save(me, request, "POST", f"/call-links/{link_id}/new-url", None, f"call link {link_id}: new address")


@router.delete("/call-links/{link_id}")
def api_delete_call_link(link_id: int, request: Request, me: Signed = Depends(admin)):
    _saving(me.user)
    r = gw.call(me.user, "DELETE", f"/call-links/{link_id}")
    _audit(me, request, "call link deleted", str(link_id))
    return _done(r)


app.include_router(router)
