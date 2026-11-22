import React, { useCallback, useEffect, useState } from "react";
import { del, get, post, put } from "./api";
import { Button, Field, Message, useAction } from "./ui";
import { goToMicrosoft } from "./microsoft";

// Tenant admins: their organisation's people. An admin manages
// everything; a user has one extension. The API keeps at least one admin.

export function Staff({ me, notice }) {
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
      {notice?.tab === "staff" && <Message kind={notice.kind}>{notice.text}</Message>}
      <Microsoft365 onSynced={load} />
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
        {u.microsoft && <span className="tag">Microsoft 365</span>}
        {u.shares_ext && <span className="tag tag-off" title="Another active person is on this extension: one person each">
          Shares ext {u.ext}</span>}
        {u.disabled && <span className="tag tag-off">Disabled</span>}
      </button>
      {open && (
        <div className="editor">
          {u.microsoft && <div className="block">
            <p className="small">{u.name || u.email}'s name, role and extension come from Microsoft 365: change them in
              {" "}<a href={MS_ADMIN[0][0]} target="_blank" rel="noreferrer">the Microsoft 365 admin centre</a> (name,
              Office phone, group) or <a href={MS_ADMIN[2][0]} target="_blank" rel="noreferrer">the Exchange admin
              centre</a> (extension, Custom attributes). You can still disable them here.</p>
          </div>}
          {!u.microsoft && <div className="block">
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
          </div>}
          <div className="buttons">
            {u.invited && <Button kind="secondary" busy={a.busy}
              onClick={() => a.run(() => post(`/users/${u.id}/invite-again`), "Invitation sent again.")}>Send the invitation again</Button>}
            {u.disabled
              ? <Button kind="secondary" busy={a.busy} onClick={() => save({ disabled: false }, "Enabled.")}>Enable</Button>
              : <Button kind="danger" busy={a.busy} onClick={() => window.confirm(
                `${self ? "Disable your own account? You'll be signed out." : `Disable ${u.email}? They're signed out at once.`}`)
                && save({ disabled: true }, "Disabled.")}>Disable</Button>}
            {!self && <Button kind="danger" busy={a.busy} onClick={() => window.confirm(
              (u.microsoft ? `${u.email} comes from your Microsoft 365 group: unless they leave it, the next sync adds them `
                + "again. " : "")
              + `Remove ${u.email}? They can't sign in any more, and their passkeys and sessions are deleted. `
              + `${u.role === "user" && u.ext ? `Extension ${u.ext} stays as it is. ` : ""}They can be invited again later.`)
              && a.run(() => del(`/users/${u.id}`), "Removed.").then((r) => r && onChange())}>Remove</Button>}
          </div>
          {a.show}
        </div>
      )}
    </li>
  );
}

// Microsoft 365: sign-in with work accounts, and Staff kept in step with a
// group. Extensions come from an Exchange custom attribute (1-15).
function Microsoft365({ onSynced }) {
  const [s, setS] = useState(null);
  const [f, setF] = useState({ group: "", admin_group: "", attribute: 1, create_exts: false });
  const [result, setResult] = useState(null);
  const a = useAction();
  const load = useCallback(() => get("/microsoft").then((r) => {
    setS(r);
    if (r.linked) setF({ group: r.group || "", admin_group: r.admin_group || "", attribute: r.attribute || 1,
      create_exts: !!r.create_exts });
    if (r.last_result) setResult(r.last_result);
  }).catch(() => setS({ available: false })), []);
  useEffect(() => { load(); }, [load]);
  if (!s || !s.available) return null;
  if (!s.linked) {
    return (
      <div className="card">
        <h3>Microsoft 365</h3>
        <p className="small muted">Let your staff sign in with their work accounts, and keep this list in step with a
          Microsoft 365 group. You sign in with your own Microsoft account to connect your organisation.</p>
        <p className="small">First, your Microsoft 365 admin approves the phone portal, once:{" "}
          <a href={s.consent_url} rel="noreferrer">Approve in Microsoft 365</a> (send them this link if that's not you).
          Then connect.</p>
        <Button kind="secondary" busy={a.busy} onClick={() => a.run(() => goToMicrosoft("/microsoft/link/start"))}>
          Connect Microsoft 365</Button>
        {a.show}
      </div>
    );
  }
  const save = () => a.run(() => put("/microsoft", f), "Saved.").then((r) => r && load());
  const run = (path) => a.run(() => post(path)).then((r) => { if (r) { setResult(r); if (path.endsWith("sync")) onSynced(); load(); } });
  return (
    <div className="card">
      <h3>Microsoft 365</h3>
      <p className="small muted">Connected{s.linked_by ? ` by ${s.linked_by}` : ""}. Staff sign in with Microsoft;
        admins still give their two-factor code. The group's members are synced every 15 minutes: people who leave it
        are disabled. Each person's extension is their Exchange <strong>custom attribute {f.attribute}</strong>
        (Exchange admin centre, Recipients, Mailboxes, their mailbox, Custom attributes).</p>
      <p className="small">First, your Microsoft 365 admin must approve the phone portal reading your groups, once:{" "}
        <a href={s.consent_url} rel="noreferrer">Approve in Microsoft 365</a>.</p>
      <Field label="Group to sync" hint="Its name or object id. Its members use the phone system.">
        <input value={f.group} onChange={(e) => setF({ ...f, group: e.target.value })} placeholder="e.g. Phone users" />
      </Field>
      <Field label="Admins group (optional)" hint="Members of both groups are admins here. Everyone else is staff.">
        <input value={f.admin_group} onChange={(e) => setF({ ...f, admin_group: e.target.value })} />
      </Field>
      <Field label="Extension is in custom attribute">
        <select value={f.attribute} onChange={(e) => setF({ ...f, attribute: Number(e.target.value) })}>
          {Array.from({ length: 15 }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </Field>
      <MicrosoftAdminLinks />
      <label className="check">
        <input type="checkbox" checked={f.create_exts} onChange={(e) => setF({ ...f, create_exts: e.target.checked })} />
        Create an extension that doesn't exist yet (one of your extension numbers), named after the person
      </label>
      <p className="small muted">Each person's extension is named after them, its voicemail is emailed to them (unless
        it's already set), and calls out show their Office phone if it's one of your numbers. A leaver's extension is
        switched off, and back on if they return.</p>
      <div className="buttons">
        <Button kind="secondary" busy={a.busy} onClick={save}>Save</Button>
        <Button kind="secondary" busy={a.busy} disabled={!s.group} onClick={() => run("/microsoft/preview")}>Preview</Button>
        <Button busy={a.busy} disabled={!s.group} onClick={() => run("/microsoft/sync")}>Sync now</Button>
        <Button kind="danger" busy={a.busy} onClick={() => window.confirm(
          "Disconnect Microsoft 365? Nobody signs in with Microsoft any more, and synced people without a password are disabled.")
          && a.run(() => del("/microsoft"), "Disconnected.").then((r) => r && (load(), onSynced()))}>Disconnect</Button>
      </div>
      {a.show}
      {result && <SyncResult r={result} />}
    </div>
  );
}

// Where an organisation changes what sync reads: Microsoft's own admin sites
// (signed in as a Microsoft 365 admin).
const MS_ADMIN = [
  ["https://admin.microsoft.com/#/users", "Microsoft 365 admin centre: users",
    "names, Office phone (caller ID), and switching an account off"],
  ["https://admin.microsoft.com/#/groups", "Microsoft 365 admin centre: groups",
    "who's in the group to sync, and the admins group"],
  ["https://admin.exchange.microsoft.com/#/mailboxes", "Exchange admin centre: mailboxes",
    "each person's extension: their mailbox, Others, Custom attributes"],
];

function MicrosoftAdminLinks() {
  return (
    <div className="small">
      <p><strong>Where to make changes</strong> (sign in with a Microsoft 365 admin account; the next sync picks them
        up, or click Sync now):</p>
      <ul>{MS_ADMIN.map(([href, label, what]) => (
        <li key={href}><a href={href} target="_blank" rel="noreferrer">{label}</a>: {what}</li>))}</ul>
    </div>
  );
}

const DOING = { add: "Add", link: "Link", change: "Update", enable: "Enable", disable: "Disable" };
const EXT_DOING = {
  create: (x) => `Create extension ${x.ext} (${x.name})`,
  rename: (x) => `Name extension ${x.ext} “${x.name}”`,
  voicemail_email: (x) => `Email extension ${x.ext}'s voicemail to ${x.email}`,
  caller_id: (x) => `Extension ${x.ext} shows ${x.number || "the main number"} on calls out`,
  switch_off: (x) => `Switch off extension ${x.ext} (${x.name})`,
  switch_on: (x) => `Switch extension ${x.ext} back on`,
};

function SyncResult({ r }) {
  if (r.error) return <Message kind="error">{r.error}</Message>;
  return (
    <div className="small">
      <p>{r.members} in the group: {r.counts.added} to add, {r.counts.changed} to update, {r.counts.enabled} to enable,
        {" "}{r.counts.disabled} to disable{r.at ? ` (${new Date(r.at).toLocaleString()})` : ""}.</p>
      {r.actions?.length > 0 && <ul>{r.actions.map((x, i) => (
        <li key={i}>{DOING[x.do]}: {x.email}{x.role === "admin" ? " (admin)" : x.ext ? ` (ext ${x.ext})` : ""}
          {x.changes ? `: ${x.changes.join(", ")}` : ""}</li>))}</ul>}
      {r.extensions?.length > 0 && <><p><strong>Extensions:</strong></p>
        <ul>{r.extensions.map((x, i) => <li key={i}>{EXT_DOING[x.do] ? EXT_DOING[x.do](x) : x.do}</li>)}</ul></>}
      {r.problems?.length > 0 && <><p><strong>Not synced:</strong></p>
        <ul>{r.problems.map((p, i) => <li key={i}>{p.who}: {p.problem}</li>)}</ul></>}
    </div>
  );
}
