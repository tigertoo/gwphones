import React, { useCallback, useEffect, useState } from "react";
import { get, post, put } from "./api";
import { Button, Field, Message, useAction } from "./ui";

// Tenant admins: their organisation's people. An admin manages
// everything; a user has one extension. The API keeps at least one admin.

export function Staff({ me }) {
  const [users, setUsers] = useState(null);
  const [exts, setExts] = useState([]);
  const [error, setError] = useState(null);
  const load = useCallback(async () => {
    try {
      const [u, e] = await Promise.all([get("/users"), get("/extensions")]);
      setUsers(u.users);
      setExts(e.extensions);
    } catch (err) {
      setError(err.message);
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  if (error) return <Message kind="error">{error}</Message>;
  if (!users) return <p className="muted">Loading…</p>;
  return (
    <section>
      <h2>Staff</h2>
      <p className="small muted">Admins manage everything here. Everyone else manages their own extension: its forwarding,
        voicemail and phone setup, and hears its recordings.</p>
      <Invite exts={exts} onDone={load} />
      <ul className="list">
        {users.map((u) => <Person key={u.id} u={u} me={me} exts={exts} onChange={load} />)}
      </ul>
    </section>
  );
}

function ExtSelect({ exts, value, onChange }) {
  return (
    <select value={value || ""} onChange={(e) => onChange(e.target.value)} aria-label="Extension">
      <option value="">Choose an extension…</option>
      {exts.map((e) => <option key={e.ext} value={e.ext}>{e.ext} {e.name}</option>)}
    </select>
  );
}

function Invite({ exts, onDone }) {
  const empty = { email: "", name: "", role: "user", ext: "" };
  const [f, setF] = useState(empty);
  const [open, setOpen] = useState(false);
  const a = useAction();
  if (!open) return <Button kind="secondary" onClick={() => setOpen(true)}>Invite someone</Button>;
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => post("/users", { ...f, ext: f.role === "user" ? f.ext : null }),
      (r) => `Invitation sent to ${r.user.email}.`);
    if (r) {
      setF(empty);
      onDone();
    }
  };
  return (
    <form className="card" onSubmit={submit}>
      <h3>Invite someone</h3>
      <Field label="Email"><input type="email" required value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
      <Field label="Name"><input maxLength={80} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      <Field label="They are">
        <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
          <option value="user">Staff, with their own extension</option>
          <option value="admin">An admin (two-factor codes required)</option>
        </select>
      </Field>
      {f.role === "user" && <Field label="Their extension"><ExtSelect exts={exts} value={f.ext} onChange={(ext) => setF({ ...f, ext })} /></Field>}
      {a.show}
      <div className="buttons">
        <Button busy={a.busy} type="submit">Send the invitation</Button>
        <Button kind="secondary" type="button" onClick={() => setOpen(false)}>Close</Button>
      </div>
    </form>
  );
}

function Person({ u, me, exts, onChange }) {
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState(u.role);
  const [ext, setExt] = useState(u.ext || "");
  const a = useAction();
  const self = u.email === me.email;
  const save = (changes, text) => a.run(() => put(`/users/${u.id}`, changes), text).then((r) => r && onChange());
  return (
    <li className={`row ${open ? "row-open" : ""}`}>
      <button className="row-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="grow">{u.name || u.email}<span className="small muted block">{u.email}</span></span>
        <span className="tag">{u.role === "admin" ? "Admin" : `Ext ${u.ext || "–"}`}</span>
        {u.invited && <span className="tag">Invited</span>}
        {u.disabled && <span className="tag tag-off">Disabled</span>}
      </button>
      {open && (
        <div className="editor">
          <div className="block">
            <Field label="Role">
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="user">Staff, with their own extension</option>
                <option value="admin">Admin</option>
              </select>
            </Field>
            {role === "user" && <Field label="Extension"><ExtSelect exts={exts} value={ext} onChange={setExt} /></Field>}
            <Button busy={a.busy} kind="secondary" disabled={role === u.role && (ext || "") === (u.ext || "")}
              onClick={() => save({ role, ext: role === "user" ? ext : null }, "Saved. They're signed out, to sign in again.")}>
              Save</Button>
          </div>
          <div className="buttons">
            {u.invited && <Button kind="secondary" busy={a.busy}
              onClick={() => a.run(() => post(`/users/${u.id}/invite-again`), "Invitation sent again.")}>Send the invitation again</Button>}
            {u.disabled
              ? <Button kind="secondary" busy={a.busy} onClick={() => save({ disabled: false }, "Enabled.")}>Enable</Button>
              : <Button kind="danger" busy={a.busy} onClick={() => window.confirm(
                `${self ? "Disable your own account? You'll be signed out." : `Disable ${u.email}? They're signed out at once.`}`)
                && save({ disabled: true }, "Disabled.")}>Disable</Button>}
          </div>
          {a.show}
        </div>
      )}
    </li>
  );
}
