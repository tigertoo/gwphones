import React, { useCallback, useEffect, useState } from "react";
import { del, get, post, put } from "./api";
import { Button, Field, Message, useAction } from "./ui";

// Desk phones (Yealink), for admins: add the phone with its MAC, then
// enter the address and login shown here on the phone's own web page.
// Each phone gets its own extension.

function seen(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function DeskPhones() {
  const [data, setData] = useState(null);
  const [exts, setExts] = useState([]);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);
  const load = useCallback(() => Promise.all([get("/desk-phones"), get("/extensions")])
    .then(([d, e]) => { setData(d); setExts(e.extensions); setError(null); })
    .catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  if (error) return <Message kind="error">{error}</Message>;
  if (!data) return <p className="muted">Loading…</p>;
  const name = (ext) => exts.find((e) => e.ext === ext)?.name || "";
  const taken = new Set(data.phones.map((p) => p.ext));
  return (
    <section>
      <h2>Desk phones</h2>
      <p className="small muted">Yealink desk phones (T4x, T3x, T5x). Add a phone here, then enter the address and login
        shown for it on the phone's web page. It then sets itself up and checks for changes every {data.repeat_minutes}{" "}
        minutes. Give each desk phone its own extension: an extension works on one device at a time.</p>
      {!data.ready && <Message kind="info">Desk phones aren't switched on for this system yet. Contact us.</Message>}
      <ul className="list">
        {data.phones.map((p) => <PhoneItem key={p.mac} p={p} data={data} exts={exts} taken={taken} name={name}
          onChange={load} />)}
      </ul>
      {adding ? <PhoneEditor data={data} exts={exts} taken={taken} onDone={() => { setAdding(false); load(); }}
        onCancel={() => setAdding(false)} />
        : <Button kind="secondary" onClick={() => setAdding(true)}>Add a desk phone</Button>}
    </section>
  );
}

function Copy({ label, value }) {
  const [done, setDone] = useState(null);
  const copy = () => navigator.clipboard.writeText(value).then(() => setDone("Copied."),
    () => setDone("Couldn't copy: select it and copy it instead."));
  return (
    <Field label={label} hint={done}>
      <div className="inline-form">
        <input readOnly value={value} aria-label={label} onFocus={(e) => e.target.select()} className="key" />
        <Button kind="secondary" onClick={copy}>Copy</Button>
      </div>
    </Field>
  );
}

function PhoneItem({ p, data, exts, taken, name, onChange }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState(null); // "change" | "config" | "remove"
  const [config, setConfig] = useState(null);
  const [newSip, setNewSip] = useState(true);
  const a = useAction();
  const fetched = seen(p.last_fetch_at);
  const showConfig = async () => {
    if (mode === "config") { setMode(null); return; }
    const r = await a.run(() => fetch(`/api/desk-phones/${p.mac}/config`, { credentials: "same-origin" })
      .then((res) => { if (!res.ok) throw new Error("Couldn't load the settings. Try again."); return res.text(); }));
    if (r !== undefined) { setConfig(r); setMode("config"); }
  };
  const newPassword = async () => {
    if (!window.confirm(`Make a new provisioning password for ${p.mac_pretty}? The phone can't fetch changes until you `
      + "enter the new one on it.")) return;
    const r = await a.run(() => post(`/desk-phones/${p.mac}/new-password`), "New password made: enter it on the phone.");
    if (r) onChange();
  };
  const remove = async () => {
    const r = await a.run(() => del(`/desk-phones/${p.mac}${newSip ? "?new_sip_password=true" : ""}`), "Removed.");
    if (r) onChange();
  };
  return (
    <li className={`row ${open ? "row-open" : ""}`}>
      <button className="row-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="ext">{p.ext}</span>
        <span className="grow">{name(p.ext) || "Extension " + p.ext}
          <span className="small muted block">{p.model || "Yealink"} · {p.mac_pretty}</span></span>
        <span className={`tag ${fetched ? "" : "tag-off"}`}>{fetched ? "Set up" : "Not set up yet"}</span>
      </button>
      {open && (
        <div className="editor">
          <p className="small muted">{fetched ? `Last fetched its settings ${fetched}${p.last_fetch_agent ? ` (${p.last_fetch_agent})` : ""}.`
            : "It hasn't fetched its settings yet."}</p>
          {p.upgrade_to && !p.upgrade_stuck && <p className="small muted">Its firmware ({p.firmware}) is being updated to {p.upgrade_to}: it
            restarts by itself, in a few minutes, when it next checks.</p>}
          <div className="block">
            <strong>On the phone's web page, Settings → Auto Provision:</strong>
            <Copy label="Server URL" value={data.server_url} />
            <Copy label="User name" value={p.username} />
            <Copy label="Password" value={p.password} />
          </div>
          <details>
            <summary>How to set up the phone</summary>
            <ol className="steps small">
              <li>Plug the phone in and find its IP address: press OK on the phone (or Menu → Status).</li>
              <li>Open that address in a browser on the same network and sign in as <code>admin</code> / <code>admin</code>{" "}
                (a new or factory-reset phone).</li>
              <li>Security → Trusted Certificates: set "Only Accept Trusted Certificates" to Disabled, and Confirm. The
                phone turns it back on once it has its settings.</li>
              <li>Settings → Auto Provision: enter the Server URL, User name and Password above, Confirm, then
                Autoprovision Now.</li>
              <li>The phone restarts with its extension. Its web page then signs in as <code>admin</code> with the
                password below.</li>
              <li>A factory reset clears these settings: enter them again after one.</li>
            </ol>
          </details>
          <div className="block">
            <strong>The phone's web page, once it's set up:</strong>
            <Copy label="User" value="admin" />
            <Copy label="Password" value={p.admin_password} />
          </div>
          {a.show}
          <div className="buttons">
            <Button kind="secondary" onClick={() => setMode(mode === "change" ? null : "change")}>Change</Button>
            <Button kind="secondary" busy={a.busy} onClick={showConfig}>{mode === "config" ? "Hide settings" : "View settings"}</Button>
            <Button kind="secondary" busy={a.busy} onClick={newPassword}>New password</Button>
            <Button kind="danger" onClick={() => setMode(mode === "remove" ? null : "remove")}>Remove</Button>
          </div>
          {mode === "config" && config !== null && <pre className="config small">{config}</pre>}
          {mode === "change" && <PhoneEditor data={data} exts={exts} taken={taken} p={p}
            onDone={() => { setMode(null); onChange(); }} onCancel={() => setMode(null)} />}
          {mode === "remove" && (
            <div className="card">
              <p>Remove {p.mac_pretty}? The phone keeps its last settings, and can still make calls, until it's factory-reset.</p>
              <label className="check"><input type="checkbox" checked={newSip} onChange={(e) => setNewSip(e.target.checked)} />
                Also give extension {p.ext} a new password, so the phone can't sign in any more (the extension's apps
                then need setting up again)</label>
              <div className="buttons">
                <Button kind="danger" busy={a.busy} onClick={remove}>Remove</Button>
                <Button kind="secondary" onClick={() => setMode(null)}>Cancel</Button>
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function PhoneEditor({ data, exts, taken, p, onDone, onCancel }) {
  const [mac, setMac] = useState("");
  const [ext, setExt] = useState(p?.ext || "");
  const [model, setModel] = useState(p?.model || "T46G");
  const a = useAction();
  const free = exts.filter((e) => e.enabled && (!taken.has(e.ext) || e.ext === p?.ext));
  const save = async () => {
    const r = await a.run(() => (p ? put(`/desk-phones/${p.mac}`, { ext, model })
      : post("/desk-phones", { mac, ext, model })), p ? "Saved: the phone picks it up within the hour, or when it restarts." : "Added.");
    if (r) onDone();
  };
  return (
    <div className="card">
      {!p && (
        <Field label="MAC address" hint="On the sticker under the phone, e.g. 00:15:65:12:34:56.">
          <input value={mac} onChange={(e) => setMac(e.target.value)} maxLength={17} autoComplete="off" />
        </Field>
      )}
      <Field label="Extension">
        <select value={ext} onChange={(e) => setExt(e.target.value)}>
          <option value="">Choose…</option>
          {free.map((e) => <option key={e.ext} value={e.ext}>{e.ext} {e.name}</option>)}
        </select>
      </Field>
      <Field label="Model">
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          {data.models.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
      </Field>
      {!free.length && <p className="small muted">Every extension already has a desk phone: add an extension first.</p>}
      {a.show}
      <div className="buttons">
        <Button busy={a.busy} disabled={!ext || (!p && !mac.trim())} onClick={save}>{p ? "Save" : "Add"}</Button>
        <Button kind="secondary" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}
