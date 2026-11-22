"""A tenant's company directory: on its desk phones (a Yealink remote
phone book, one per site, the phone's own site first, then its outside
Contacts) and as names for its incoming calls.

Each entry is an extension: its name and, from Microsoft 365 (the
portal's sync, PUT /api/pbx/directory), the person's department, mobile
and direct number (Office phone). An incoming call from one of those
numbers shows "Name (mobile)" or "Name (direct)" instead of the number,
looked up in the tenant's own directory only ([t<code>-cnam], generated
into the dialplan; the main tenant's [portal-cnam]).

The phone book is served like the phone's settings: /prov/yealink/
phonebook/<n>.xml, with the phone's own credentials, under its own
tenant's address only.
"""
import re
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pbx import (CID_RE, MAIN_TENANT, TenantNames, _lock, _now, apply_config, current_tenant, e164, get_db,
                 tenant_names)

router = APIRouter(prefix="/api/pbx")
MAX_BOOKS = 5  # Yealink's remote phone books (remote_phonebook.data.1-5)
DEPT_MAX = 40
MAX_CONTACTS = 2000  # a tenant's outside contacts


def national(number: str) -> str:
    """+353871234567 as 0871234567 (what a phone here dials); others in full."""
    d = e164(number)
    if not d:
        return ""
    return "0" + d[3:] if d.startswith("353") else "+" + d


def clean_text(value: str, limit: int = DEPT_MAX) -> str:
    """A name or department safe for a phone's screen and the dialplan: no
    commas, quotes, brackets of the dialplan's own or control characters."""
    v = re.sub(r"[\x00-\x1f\x7f,;\"\\\\${}\[\]|]", " ", value or "")
    return re.sub(r"\s+", " ", v).strip()[:limit]


def entries(conn, tenant: int) -> List[dict]:
    rows = conn.execute("SELECT e.ext, e.name, e.site_id, d.mobile, d.ddi, d.department FROM extensions e "
                        "LEFT JOIN directory_extras d ON d.tenant_id = e.tenant_id AND d.ext = e.ext "
                        "WHERE e.tenant_id = ? AND e.enabled = 1 ORDER BY e.name COLLATE NOCASE, e.ext",
                        (tenant,)).fetchall()
    return [{"ext": r["ext"], "name": r["name"], "site_id": r["site_id"], "mobile": r["mobile"] or "",
             "ddi": r["ddi"] or "", "department": r["department"] or ""} for r in rows]


def books(conn, tenant: int, own_site: Optional[int]) -> List[dict]:
    """The phone books a phone of this site gets: its own site's first,
    then the others, then everyone on no site; one "Everyone" book when
    there are no sites, or more than MAX_BOOKS."""
    sites = conn.execute("SELECT id, name FROM sites WHERE tenant_id = ? ORDER BY name", (tenant,)).fetchall()
    all_e = entries(conn, tenant)
    unsited = any(e["site_id"] is None for e in all_e)
    has_contacts = bool(conn.execute("SELECT 1 FROM contacts WHERE tenant_id = ?", (tenant,)).fetchone())
    out = [{"name": s["name"], "site": s["id"]} for s in sites]
    out.sort(key=lambda b: b["site"] != own_site)
    if unsited and sites:
        out.append({"name": "Others", "site": None})
    room = MAX_BOOKS - (1 if has_contacts else 0)
    if not sites or len(out) > room:
        out = [{"name": "Everyone", "site": "all"}]
    return out + ([{"name": "Contacts", "site": "contacts"}] if has_contacts else [])


def contacts(conn, tenant: int) -> List[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name, company, work, mobile, other, source FROM contacts "
                                          "WHERE tenant_id = ? ORDER BY lower(name || company), id", (tenant,))]


def contact_label(c: dict) -> str:
    """John Smith (Acme), John Smith, or Acme."""
    if c["name"] and c["company"]:
        return f"{c['name']} ({c['company']})"
    return c["name"] or c["company"]


def contacts_xml(conn, tenant: int) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<YealinkIPPhoneDirectory>"]
    for c in contacts(conn, tenant):
        out += ["  <DirectoryEntry>", f"    <Name>{escape(contact_label(c))}</Name>"]
        for key, label in (("work", "Work"), ("mobile", "Mobile"), ("other", "Other")):
            if c[key]:
                out.append(f'    <Telephone label="{label}">{escape(c[key])}</Telephone>')
        out.append("  </DirectoryEntry>")
    return "\n".join(out + ["</YealinkIPPhoneDirectory>", ""])


def book_xml(conn, tenant: int, book: dict) -> str:
    if book["site"] == "contacts":
        return contacts_xml(conn, tenant)
    show_site = book["site"] == "all" and conn.execute("SELECT 1 FROM sites WHERE tenant_id = ?", (tenant,)).fetchone()
    sites = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM sites WHERE tenant_id = ?", (tenant,))}
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<YealinkIPPhoneDirectory>"]
    for e in entries(conn, tenant):
        if book["site"] != "all" and e["site_id"] != book["site"]:
            continue
        label = " - ".join(x for x in (clean_text(e["name"], 40), e["department"],
                                       sites.get(e["site_id"], "") if show_site else "") if x)
        out += ["  <DirectoryEntry>", f"    <Name>{escape(label)}</Name>",
                f'    <Telephone label="Extension">{escape(e["ext"])}</Telephone>']
        if e["mobile"]:
            out.append(f'    <Telephone label="Mobile">{escape(e["mobile"])}</Telephone>')
        if e["ddi"]:
            out.append(f'    <Telephone label="Direct">{escape(e["ddi"])}</Telephone>')
        out.append("  </DirectoryEntry>")
    return "\n".join(out + ["</YealinkIPPhoneDirectory>", ""])


def caller_names(conn, tenant: int) -> Dict[str, str]:
    """The tenant's own outside numbers its people call from (as 353...),
    and the name to show for them."""
    out = {}
    for e in entries(conn, tenant):
        for number, what in ((e["mobile"], "mobile"), (e["ddi"], "direct")):
            d = e164(number)
            if d and d not in out:
                out[d] = clean_text(f"{e['name']} ({what})", 50)
    for c in contacts(conn, tenant):  # the tenant's own people first: a contact never renames one of them
        for key in ("work", "mobile", "other"):
            d = e164(c[key])
            if d and d not in out:
                out[d] = clean_text(contact_label(c), 50)
    return out


def cnam_context(conn, t: TenantNames) -> List[str]:
    """[<prefix>-cnam]: Gosub'd for an incoming call; each known number in
    the forms a carrier may send (353..., 0..., +353..., 00353...) sets the
    caller's name. Nothing for numbers it doesn't know."""
    names = caller_names(conn, t.id)
    out = [f"; ---- Names for incoming calls from the tenant's own people (directory.py) ----",
           f"[{t.prefix}-cnam]"]
    for d, name in sorted(names.items()):
        forms = {d, "+" + d, "00" + d} | ({"0" + d[3:]} if d.startswith("353") else set())
        for f in sorted(forms):
            out += [f"exten => {f},1,Set(CALLERID(name)={name})", " same => n,Return()"]
    return out


def lookup_line(t: TenantNames) -> str:
    """For [portal-inbound]: the caller's name from the tenant's directory."""
    # Only a number it knows: a Gosub to one that isn't there would end the call.
    ctx = f"{t.prefix}-cnam"
    return (f" same => n,GosubIf($[${{DIALPLAN_EXISTS({ctx},${{CALLERID(num)}},1)}}]"
            f"?{ctx},${{CALLERID(num)}},1)")


# -- the portal's sync -------------------------------------------------------------------------

class DirectoryEntry(BaseModel):
    ext: str
    mobile: str = ""
    ddi: str = ""
    department: str = ""


class DirectoryRequest(BaseModel):
    entries: List[DirectoryEntry]


@router.put("/directory")
def api_set_directory(req: DirectoryRequest):
    """Replaces the tenant's directory details (from Microsoft 365): only
    for its own extensions, numbers as phone numbers."""
    tid = current_tenant()
    with _lock, get_db() as conn:
        exts = {r["ext"] for r in conn.execute("SELECT ext FROM extensions WHERE tenant_id = ?", (tid,))}
        rows = []
        for e in req.entries[:2000]:
            if e.ext not in exts:
                continue
            mobile, ddi = national(e.mobile), national(e.ddi)
            for n in (mobile, ddi):
                if n and not CID_RE.match(n):
                    raise HTTPException(400, f"{n} isn't a phone number.")
            rows.append((tid, e.ext, mobile, ddi, clean_text(e.department)))
        before = conn.execute("SELECT ext, mobile, ddi, department FROM directory_extras WHERE tenant_id = ? "
                              "ORDER BY ext", (tid,)).fetchall()
        conn.execute("DELETE FROM directory_extras WHERE tenant_id = ?", (tid,))
        conn.executemany("INSERT OR REPLACE INTO directory_extras (tenant_id, ext, mobile, ddi, department) "
                         "VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
        if [tuple(r) for r in before] == sorted(r[1:] for r in rows):
            return {"ok": True, "changed": False, "entries": len(rows)}
        result = apply_config(conn, f"Directory: {len(rows)} entries from Microsoft 365")
    return {**result, "changed": True, "entries": len(rows)}


# -- outside contacts ---------------------------------------------------------------------------

class ContactRequest(BaseModel):
    name: str = ""
    company: str = ""
    work: str = ""
    mobile: str = ""
    other: str = ""


def _clean_contact(req: ContactRequest) -> dict:
    v = {"name": clean_text(req.name), "company": clean_text(req.company)}
    if not (v["name"] or v["company"]):
        raise HTTPException(400, "Give the contact a name or a company.")
    for key in ("work", "mobile", "other"):
        raw = getattr(req, key).strip()
        n = national(raw) if raw else ""
        if raw and (not n or not CID_RE.match(n)):
            raise HTTPException(400, f"{raw} isn't a phone number.")
        v[key] = n
    if not (v["work"] or v["mobile"] or v["other"]):
        raise HTTPException(400, "Give the contact at least one number.")
    return v


def _contacts_changed(conn, why: str):
    conn.commit()
    return apply_config(conn, why)  # the names for incoming calls


@router.get("/contacts")
def api_contacts():
    with get_db() as conn:
        return {"contacts": contacts(conn, current_tenant())}


@router.post("/contacts")
def api_add_contact(req: ContactRequest):
    v = _clean_contact(req)
    tid = current_tenant()
    with _lock, get_db() as conn:
        if conn.execute("SELECT COUNT(*) FROM contacts WHERE tenant_id = ?", (tid,)).fetchone()[0] >= MAX_CONTACTS:
            raise HTTPException(409, f"That's {MAX_CONTACTS} contacts already.")
        cid = conn.execute("INSERT INTO contacts (tenant_id, name, company, work, mobile, other, source, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, 'portal', ?)", (tid, v["name"], v["company"], v["work"],
                                                                     v["mobile"], v["other"], _now())).lastrowid
        result = _contacts_changed(conn, f"Contact added: {contact_label(v)}")
    return {"id": cid, **result}


def _own_contact(conn, contact_id: int):
    r = conn.execute("SELECT * FROM contacts WHERE id = ? AND tenant_id = ?", (contact_id, current_tenant())).fetchone()
    if not r:
        raise HTTPException(404, "Contact not found.")
    if r["source"] != "portal":
        raise HTTPException(409, "This contact comes from Microsoft 365: change it there.")
    return r


class MicrosoftContacts(BaseModel):
    contacts: List[ContactRequest]


@router.put("/contacts/microsoft")
def api_microsoft_contacts(req: MicrosoftContacts):
    """Replaces the tenant's contacts from Microsoft 365 (Exchange's
    organisation contacts, the portal's sync). Its own (portal) ones stay.
    A contact without a usable number is left out."""
    tid = current_tenant()
    rows = []
    for c in req.contacts[:MAX_CONTACTS]:
        try:
            rows.append(_clean_contact(c))
        except HTTPException:
            continue
    with _lock, get_db() as conn:
        key = lambda x: (x["name"], x["company"], x["work"], x["mobile"], x["other"])  # noqa: E731
        before = sorted(key(dict(r)) for r in conn.execute(
            "SELECT * FROM contacts WHERE tenant_id = ? AND source = 'microsoft'", (tid,)))
        if before == sorted(key(r) for r in rows):
            return {"ok": True, "changed": False, "contacts": len(rows)}
        conn.execute("DELETE FROM contacts WHERE tenant_id = ? AND source = 'microsoft'", (tid,))
        conn.executemany("INSERT INTO contacts (tenant_id, name, company, work, mobile, other, source, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, 'microsoft', ?)",
                         [(tid, r["name"], r["company"], r["work"], r["mobile"], r["other"], _now()) for r in rows])
        result = _contacts_changed(conn, f"Contacts from Microsoft 365: {len(rows)}")
    return {**result, "changed": True, "contacts": len(rows)}


@router.put("/contacts/{contact_id}")
def api_change_contact(contact_id: int, req: ContactRequest):
    v = _clean_contact(req)
    with _lock, get_db() as conn:
        _own_contact(conn, contact_id)
        conn.execute("UPDATE contacts SET name = ?, company = ?, work = ?, mobile = ?, other = ?, updated_at = ? "
                     "WHERE id = ?", (v["name"], v["company"], v["work"], v["mobile"], v["other"], _now(), contact_id))
        return _contacts_changed(conn, f"Contact changed: {contact_label(v)}")


@router.delete("/contacts/{contact_id}")
def api_delete_contact(contact_id: int):
    with _lock, get_db() as conn:
        r = _own_contact(conn, contact_id)
        conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return _contacts_changed(conn, f"Contact removed: {contact_label(dict(r))}")


@router.get("/directory")
def api_directory():
    tid = current_tenant()
    with get_db() as conn:
        return {"entries": entries(conn, tid)}
