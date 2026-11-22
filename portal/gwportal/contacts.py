"""A tenant's outside contacts (suppliers, customers), for its admins: on
its phones' Contacts book and as names for its incoming calls (gwphones
directory.py). Those from Microsoft 365 (its sync) are changed there."""
from fastapi import Depends, Request
from pydantic import BaseModel

from . import auth, db, gw
from .app import Signed, app, client_ip, signed_in

KEEP = ("id", "name", "company", "work", "mobile", "other", "source")


def _admin(me: Signed):
    if me.user["role"] != "admin":
        raise auth.Refused("Only your organisation's admins can do that.", 403)


def _audit(me: Signed, request: Request, action: str, detail: str = ""):
    with db.connect() as conn:
        db.audit(conn, action, me.user, client_ip(request), detail)


class Contact(BaseModel):
    name: str = ""
    company: str = ""
    work: str = ""
    mobile: str = ""
    other: str = ""


@app.get("/api/contacts")
def api_contacts(me: Signed = Depends(signed_in())):
    _admin(me)
    return {"contacts": [{k: c.get(k) for k in KEEP} for c in gw.call(me.user, "GET", "/contacts")["contacts"]]}


@app.post("/api/contacts")
def api_add_contact(req: Contact, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    gw.call(me.user, "POST", "/contacts", json=req.dict())
    _audit(me, request, "contact added", f"{req.name} {req.company}".strip())
    return {"ok": True}


@app.put("/api/contacts/{contact_id}")
def api_change_contact(contact_id: int, req: Contact, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    gw.call(me.user, "PUT", f"/contacts/{contact_id}", json=req.dict())
    _audit(me, request, "contact changed", f"{contact_id}: {req.name} {req.company}".strip())
    return {"ok": True}


@app.delete("/api/contacts/{contact_id}")
def api_delete_contact(contact_id: int, request: Request, me: Signed = Depends(signed_in())):
    _admin(me)
    gw.call(me.user, "DELETE", f"/contacts/{contact_id}")
    _audit(me, request, "contact removed", str(contact_id))
    return {"ok": True}
