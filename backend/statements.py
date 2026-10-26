"""
Phone billing, stage 2: monthly statements (/api/billing/statements*).

Issuing a statement for a customer and month locks it: the calls on it get
their price written down (calls.price_micro) and are tied to it
(calls.statement_id), so a later rate sheet or markup change never alters
what was sent, and its phone numbers are recorded as billed for that month.
A statement isn't issued while the customer has calls for the month that
aren't priced, or are only provisionally priced. Everything a statement
shows is kept as a snapshot, so its PDF is always the same document.

Calls read after a statement was issued go on a follow-up statement for
the same month. Cancelling a statement unlocks its calls and numbers so
they can be issued again; the cancelled statement and its number stay.

These are statements, not VAT invoices: the business isn't VAT-registered
yet. The totals keep a VAT line (zero for now) so invoices can follow.
"""

import io
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import billing
from billing import LOCAL_TZ, MONTH_RE, _set_state, _state, from_micro, get_db, month_statement, to_micro
from pbx import EMAIL_RE, _graph_ready, _graph_send, changes_log

router = APIRouter(prefix="/api/billing")

SETTINGS = {  # key: (label, max length)
    "business_name": ("Business name", 100),
    "address": ("Address", 400),
    "email": ("Email", 120),
    "phone": ("Phone", 40),
    "payment_details": ("How to pay (e.g. IBAN, BIC, reference)", 600),
    "footer": ("Footer note", 400),
}


def _settings(conn) -> dict:
    raw = _state(conn, "statement_settings", "")
    saved = json.loads(raw) if raw else {}
    return {k: saved.get(k, "") for k in SETTINGS}


class SettingsRequest(BaseModel):
    business_name: str = ""
    address: str = ""
    email: str = ""
    phone: str = ""
    payment_details: str = ""
    footer: str = ""


@router.get("/statement-settings")
def api_get_settings():
    with get_db() as conn:
        return {"settings": _settings(conn), "fields": {k: v[0] for k, v in SETTINGS.items()},
                "email_ready": _graph_ready()}


@router.put("/statement-settings")
def api_put_settings(req: SettingsRequest):
    values = req.model_dump()
    for k, (label, limit) in SETTINGS.items():
        values[k] = values[k].strip()
        if len(values[k]) > limit:
            raise HTTPException(400, f"{label} is too long (up to {limit} characters).")
    if values["email"] and not EMAIL_RE.match(values["email"]):
        raise HTTPException(400, "That doesn't look like an email address.")
    with billing._lock, get_db() as conn:
        _set_state(conn, "statement_settings", json.dumps(values))
        conn.commit()
    changes_log.info("Billing: statement details updated")
    return {"ok": True, "messages": ["Statement details saved."]}


# ---------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------

def _money2(micro: int) -> str:
    return from_micro(micro, 2)


def _issue_one(conn, entry: dict, month: str, settings: dict) -> Optional[dict]:
    """Locks and snapshots one customer's unbilled items for the month.
    Returns the statement row's fields, or None if there's nothing to bill."""
    calls = [c for c in entry["calls"] if c["statement_id"] is None]
    ddis = [d for d in entry["ddis"] if d["statement_id"] is None]
    if not calls and not ddis:
        return None
    name = entry["customer"]["name"]
    if any(c["status"] in ("unrated", "provisional") for c in calls):
        raise HTTPException(409, f"{name} has calls in {month} that aren't priced, or are only provisionally "
                                 "priced. Upload the missing or newer rate sheet first.")
    call_lines = [{"id": c["id"], "start": c["start"], "ext": c["ext"] or "",
                   "number": f"+{c['number']}" if c["number"] else (c["dialled"] or ""),
                   "destination": c["destination"] or "", "seconds": c["billsec"],
                   "price_micro": to_micro(Decimal(c["price_eur"]))} for c in calls]
    groups = {}
    for c in call_lines:
        g = groups.setdefault(c["destination"] or "Other", {"destination": c["destination"] or "Other",
                                                            "calls": 0, "seconds": 0, "price_micro": 0})
        g["calls"] += 1
        g["seconds"] += c["seconds"]
        g["price_micro"] += c["price_micro"]
    ddi_lines = [{"did": d["did"], "label": d["label"], "price_micro": to_micro(Decimal(d["price_eur"]))} for d in ddis]
    calls_total = sum(c["price_micro"] for c in call_lines)
    ddis_total = sum(d["price_micro"] for d in ddi_lines)
    # Rounded to the cent once, on the total, so the lines add up as shown.
    subtotal = to_micro(Decimal(_money2(calls_total + ddis_total)))
    seq = int(_state(conn, "statement_seq", "0") or 0) + 1
    _set_state(conn, "statement_seq", seq)
    number = f"S{seq:05d}"
    issued = datetime.now(timezone.utc)
    snapshot = {
        "number": number, "month": month, "issued": issued.astimezone(LOCAL_TZ).strftime("%d %b %Y"),
        "business": settings,
        "customer": {"name": name, "email": entry["customer"]["email"] or ""},
        "follow_up": bool(conn.execute(
            "SELECT 1 FROM statements WHERE customer_id IS ? AND month = ? AND cancelled_at IS NULL",
            (entry["customer"]["id"], month)).fetchone()),
        "ddis": ddi_lines, "groups": sorted(groups.values(), key=lambda g: -g["price_micro"]), "calls": call_lines,
        "totals": {"ddis": ddis_total, "calls": calls_total, "subtotal": subtotal, "vat": 0, "total": subtotal},
    }
    cur = conn.execute("INSERT INTO statements (number, customer_id, month, issued_at, total_micro, snapshot) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (number, entry["customer"]["id"], month, issued.isoformat(timespec="seconds"), subtotal,
                        json.dumps(snapshot)))
    sid = cur.lastrowid
    for c in call_lines:
        conn.execute("UPDATE calls SET statement_id = ?, price_micro = ? WHERE id = ?", (sid, c["price_micro"], c["id"]))
    conn.executemany("INSERT INTO statement_ddis (statement_id, did, month) VALUES (?, ?, ?)",
                     [(sid, d["did"], month) for d in ddi_lines])
    return {"id": sid, "number": number, "customer": name, "total": _money2(subtotal)}


def _month_ended(month: str) -> bool:
    return month < datetime.now(LOCAL_TZ).strftime("%Y-%m")


class IssueRequest(BaseModel):
    month: str
    customer_id: Optional[int] = None  # None = every customer with something to bill


@router.post("/statements")
def api_issue(req: IssueRequest):
    if not MONTH_RE.match(req.month):
        raise HTTPException(400, "Month must look like 2026-10.")
    if not _month_ended(req.month):
        raise HTTPException(400, "Statements are for months that have ended.")
    billing.import_cdr()  # don't leave the last calls of the month out
    billing.rate_calls()
    with billing._lock, get_db() as conn:
        settings = _settings(conn)
        if not settings["business_name"]:
            raise HTTPException(400, "Fill in your business details for statements first.")
        entries = [e for e in month_statement(conn, req.month)
                   if e["customer"]["id"] is not None
                   and (req.customer_id is None or e["customer"]["id"] == req.customer_id)]
        if req.customer_id is not None and not entries:
            raise HTTPException(404, "Customer not found.")
        # Check every customer before issuing any, so a bulk run is all or nothing.
        for e in entries:
            pending = [c for c in e["calls"] if c["statement_id"] is None and c["status"] in ("unrated", "provisional")]
            if pending:
                raise HTTPException(409, f"{e['customer']['name']} has {len(pending)} call(s) in {req.month} that "
                                         "aren't priced, or are only provisionally priced. Upload the missing or "
                                         "newer rate sheet first.")
        issued = [s for s in (_issue_one(conn, e, req.month, settings) for e in entries) if s]
        conn.commit()
    for s in issued:
        changes_log.info(f"Billing: issued statement {s['number']} to {s['customer']} for {req.month}: €{s['total']}")
    if not issued:
        return {"ok": True, "issued": [], "messages": [f"Nothing new to bill for {req.month}."]}
    return {"ok": True, "issued": issued,
            "messages": [f"Issued {len(issued)} statement(s) for {req.month}: "
                         + ", ".join(f"{s['number']} {s['customer']} €{s['total']}" for s in issued)]}


@router.get("/statements")
def api_list(month: str = ""):
    with get_db() as conn:
        sql, args = "SELECT * FROM statements", []
        if month:
            sql, args = sql + " WHERE month = ?", [month]
        rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
    out = []
    for r in rows:
        snap = json.loads(r["snapshot"])
        out.append({"id": r["id"], "number": r["number"], "month": r["month"], "customer": snap["customer"]["name"],
                    "customer_email": snap["customer"]["email"], "total": _money2(r["total_micro"]),
                    "issued_at": r["issued_at"], "emailed_at": r["emailed_at"], "emailed_to": r["emailed_to"],
                    "cancelled_at": r["cancelled_at"], "follow_up": snap.get("follow_up", False)})
    return {"statements": out}


@router.post("/statements/{sid}/cancel")
def api_cancel(sid: int):
    with billing._lock, get_db() as conn:
        r = conn.execute("SELECT * FROM statements WHERE id = ?", (sid,)).fetchone()
        if not r:
            raise HTTPException(404, "Statement not found.")
        if r["cancelled_at"]:
            raise HTTPException(409, "Already cancelled.")
        conn.execute("UPDATE statements SET cancelled_at = ? WHERE id = ?", (billing._now(), sid))
        conn.execute("UPDATE calls SET statement_id = NULL, price_micro = NULL WHERE statement_id = ?", (sid,))
        conn.execute("DELETE FROM statement_ddis WHERE statement_id = ?", (sid,))
        conn.commit()
    changes_log.info(f"Billing: cancelled statement {r['number']}")
    return {"ok": True, "messages": [f"{r['number']} cancelled. Its calls and numbers can be billed again."]}


# ---------------------------------------------------------------------------
# The PDF
# ---------------------------------------------------------------------------

def _month_name(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def _duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def statement_pdf(snap: dict, cancelled: bool = False) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    title = ParagraphStyle("title", parent=styles["Title"], fontSize=18, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
    p = lambda text, style=body: Paragraph(escape(text).replace("\n", "<br/>"), style)
    b, c = snap["business"], snap["customer"]
    t = snap["totals"]
    eur = lambda micro: f"€{_money2(micro)}"

    story = []
    head_left = [p(b["business_name"], ParagraphStyle("biz", parent=body, fontSize=12, leading=15))]
    if b["address"]:
        head_left.append(p(b["address"]))
    contact = " · ".join(x for x in (b["email"], b["phone"]) if x)
    if contact:
        head_left.append(p(contact, small))
    head_right = [p(("CANCELLED " if cancelled else "") + "Statement", title),
                  p(f"Number: {snap['number']}"), p(f"Date: {snap['issued']}"),
                  p(f"Period: {_month_name(snap['month'])}" + (" (follow-up)" if snap.get("follow_up") else ""))]
    head = Table([[head_left, head_right]], colWidths=[105 * mm, 65 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 8 * mm), p("To:", small), p(c["name"]), Spacer(1, 6 * mm)]

    def table(rows, widths, money_cols=(), total_row=False):
        tbl = Table(rows, colWidths=[w * mm for w in widths], repeatRows=1)
        style = [("FONTSIZE", (0, 0), (-1, -1), 8.5), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                 ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#999999")),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3)]
        for col in money_cols:
            style.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
        if total_row:
            style += [("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#999999")),
                      ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]
        tbl.setStyle(TableStyle(style))
        return tbl

    summary = [["", "Amount"]]
    if snap["ddis"]:
        summary.append(["Phone numbers", eur(t["ddis"])])
    if snap["calls"]:
        summary.append([f"Calls ({len(snap['calls'])})", eur(t["calls"])])
    summary.append(["Total due", eur(t["total"])])
    story += [p("Summary", h2), table(summary, [140, 30], money_cols=(1,), total_row=True)]
    story.append(p("VAT not charged: the supplier is not VAT-registered.", small))

    if snap["ddis"]:
        rows = [["Number", "Description", "Amount"]] + [[f"+{d['did']}", d["label"] or "Phone number, monthly",
                                                         eur(d["price_micro"])] for d in snap["ddis"]]
        story += [p("Phone numbers", h2), table(rows, [45, 95, 30], money_cols=(2,))]
    if snap["calls"]:
        rows = [["Destination", "Calls", "Time", "Amount"]] + [
            [g["destination"], str(g["calls"]), _duration(g["seconds"]), eur(g["price_micro"])] for g in snap["groups"]]
        story += [p("Calls by destination", h2), table(rows, [100, 20, 20, 30], money_cols=(1, 2, 3))]
        rows = [["Date and time", "Ext", "Number", "Destination", "Time", "Amount"]] + [
            [x["start"][:16], x["ext"], x["number"], x["destination"][:34], _duration(x["seconds"]),
             f"€{from_micro(x['price_micro'], 3)}"] for x in snap["calls"]]
        story += [p("Itemised calls", h2), table(rows, [32, 12, 36, 52, 16, 22], money_cols=(4, 5))]
    if b["payment_details"]:
        story += [p("How to pay", h2), p(b["payment_details"])]
    if b["footer"]:
        story += [Spacer(1, 6 * mm), p(b["footer"], small)]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=f"Statement {snap['number']}", author=b["business_name"])
    doc.build(story)
    return buf.getvalue()


def _statement(sid: int):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM statements WHERE id = ?", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "Statement not found.")
    return r, json.loads(r["snapshot"])


def _filename(snap) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "-", snap["customer"]["name"]).strip("-") or "customer"
    return f"Statement-{snap['number']}-{name}-{snap['month']}.pdf"


@router.get("/statements/{sid}/pdf")
def api_pdf(sid: int):
    r, snap = _statement(sid)
    return Response(statement_pdf(snap, bool(r["cancelled_at"])), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{_filename(snap)}"'})


class EmailRequest(BaseModel):
    to: str = ""  # empty = the customer's email


@router.post("/statements/{sid}/email")
def api_email(sid: int, req: EmailRequest):
    r, snap = _statement(sid)
    if r["cancelled_at"]:
        raise HTTPException(409, "That statement is cancelled.")
    to = req.to.strip() or snap["customer"]["email"]
    if not to or not EMAIL_RE.match(to):
        raise HTTPException(400, "Give the customer an email address first (or enter one).")
    if not _graph_ready():
        raise HTTPException(400, "Email isn't set up (Microsoft Graph settings in the dashboard's .env).")
    b = snap["business"]
    esc = lambda s: escape(s or "")
    body = f"""<div style="font:15px -apple-system,Segoe UI,sans-serif;color:#222;max-width:560px">
<p>Hello,</p>
<p>Please find attached your phone statement <b>{esc(snap['number'])}</b> for {esc(_month_name(snap['month']))}.
The total due is <b>€{_money2(snap['totals']['total'])}</b>.</p>
{f"<p>{esc(b['payment_details']).replace(chr(10), '<br>')}</p>" if b['payment_details'] else ""}
<p>Thank you,<br>{esc(b['business_name'])}</p>
</div>"""
    _graph_send(to, f"Phone statement {snap['number']} – {_month_name(snap['month'])}", body, {},
                [{"name": _filename(snap), "type": "application/pdf", "data": statement_pdf(snap)}])
    with billing._lock, get_db() as conn:
        conn.execute("UPDATE statements SET emailed_at = ?, emailed_to = ? WHERE id = ?", (billing._now(), to, sid))
        conn.commit()
    changes_log.info(f"Billing: emailed statement {snap['number']} to {to}")
    return {"ok": True, "messages": [f"{snap['number']} emailed to {to}."]}
