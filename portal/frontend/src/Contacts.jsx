import React, { useCallback, useEffect, useState } from "react";
import { del, get, post, put } from "./api";
import { Button, Field, Message, useAction } from "./ui";

// Outside contacts (suppliers, customers): on every phone's Contacts book,
// and their name shows when they ring. Admins only. Those from Microsoft
// 365 (Staff, Microsoft 365) are changed there.
const EMPTY = { name: "", company: "", work: "", mobile: "", other: "" };

export function Contacts() {
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null); // a contact, or EMPTY for a new one
  const [q, setQ] = useState("");
  const load = useCallback(() => get("/contacts").then((r) => { setList(r.contacts); setError(null); })
    .catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  if (error) return <Message kind="error">{error}</Message>;
  if (!list) return <p className="muted">Loading…</p>;
  const shown = list.filter((c) => !q || `${c.name} ${c.company} ${c.work} ${c.mobile} ${c.other}`.toLowerCase()
    .includes(q.toLowerCase()));
  return (
    <section>
      <h2>Contacts</h2>
      <p className="small muted">Your organisation's outside contacts: suppliers, customers, anyone you call often. They're
        in every desk phone's Contacts book, and their name shows when they ring.</p>
      {editing ? <ContactForm contact={editing} onDone={() => { setEditing(null); load(); }}
        onCancel={() => setEditing(null)} /> : <Button kind="secondary" onClick={() => setEditing(EMPTY)}>Add a contact</Button>}
      {list.length > 8 && <input className="search" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />}
      {list.length === 0 ? <p className="muted">No contacts yet.</p> : (
        <ul className="list compact">
          {shown.map((c) => (
            <li key={c.id} className="row-head static">
              <span className="grow">{c.name || c.company}{c.name && c.company && <span className="muted"> · {c.company}</span>}
                <span className="small muted block">{[c.work && `Work ${c.work}`, c.mobile && `Mobile ${c.mobile}`,
                  c.other && `Other ${c.other}`].filter(Boolean).join(" · ")}</span></span>
              {c.source === "microsoft" ? <span className="tag">Microsoft 365</span>
                : <Button kind="secondary" onClick={() => setEditing(c)}>Edit</Button>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ContactForm({ contact, onDone, onCancel }) {
  const [f, setF] = useState({ ...EMPTY, ...contact });
  const a = useAction();
  const body = { name: f.name, company: f.company, work: f.work, mobile: f.mobile, other: f.other };
  const save = () => a.run(() => (contact.id ? put(`/contacts/${contact.id}`, body) : post("/contacts", body)), "Saved.")
    .then((r) => r && onDone());
  const remove = () => window.confirm(`Remove ${contact.name || contact.company}?`)
    && a.run(() => del(`/contacts/${contact.id}`), "Removed.").then((r) => r && onDone());
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <div className="card">
      <Field label="Name"><input maxLength={40} value={f.name} onChange={set("name")} placeholder="e.g. John Smith" /></Field>
      <Field label="Company"><input maxLength={40} value={f.company} onChange={set("company")} placeholder="e.g. Acme Supplies" /></Field>
      <Field label="Work number"><input inputMode="tel" value={f.work} onChange={set("work")} placeholder="e.g. 061 555 1234" /></Field>
      <Field label="Mobile"><input inputMode="tel" value={f.mobile} onChange={set("mobile")} /></Field>
      <Field label="Other number"><input inputMode="tel" value={f.other} onChange={set("other")} /></Field>
      <div className="buttons">
        <Button busy={a.busy} onClick={save} disabled={!(f.name.trim() || f.company.trim())}>Save</Button>
        <Button kind="secondary" onClick={onCancel}>Cancel</Button>
        {contact.id && <Button kind="danger" busy={a.busy} onClick={remove}>Remove</Button>}
      </div>
      {a.show}
    </div>
  );
}
