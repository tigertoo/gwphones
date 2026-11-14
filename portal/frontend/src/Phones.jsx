import React, { useCallback, useEffect, useState } from "react";
import { ApiError, api, get, post, put } from "./api";
import { Button, Field, Message, useAction } from "./ui";

// The phone pages: extensions (an admin's whole list, or a user's own),
// Status, and Recordings. What each person may do is the API's to say:
// these pages only leave out what can't apply (a user doesn't see the
// on/off switch, or Delete on recordings).

export function Extensions({ me }) {
  const [list, setList] = useState(null);
  const [forwarding, setForwarding] = useState({});
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(null);
  const load = useCallback(async () => {
    try {
      const [e, f] = await Promise.all([get("/extensions"), get("/forwarding").catch(() => ({ extensions: {} }))]);
      setList(e.extensions);
      setForwarding(f.extensions || {});
      if (me.role !== "admin" && e.extensions[0]) setOpen(e.extensions[0].ext);
    } catch (err) {
      setError(err.message);
    }
  }, [me.role]);
  useEffect(() => { load(); }, [load]);

  if (error) return <Message kind="error">{error}</Message>;
  if (!list) return <p className="muted">Loading…</p>;
  const admin = me.role === "admin";
  return (
    <section>
      <h2>{admin ? "Extensions" : "My phone"}</h2>
      {admin && <NewExtension onMade={load} />}
      {list.length === 0 && <p className="muted">No extensions yet.</p>}
      <ul className="list">
        {list.map((e) => (
          <li key={e.ext} className={`row ${open === e.ext ? "row-open" : ""}`}>
            <button className="row-head" onClick={() => setOpen(open === e.ext ? null : e.ext)} aria-expanded={open === e.ext}>
              <span className="ext">{e.ext}</span>
              <span className="grow">{e.name}</span>
              {!e.enabled && <span className="tag tag-off">Off</span>}
              {forwarding[e.ext]?.dnd && <span className="tag">Do not disturb</span>}
              {forwarding[e.ext]?.always && <span className="tag">Forwarded</span>}
            </button>
            {open === e.ext && <ExtensionEditor ext={e} me={me} fwd={forwarding[e.ext]} onChange={load} />}
          </li>
        ))}
      </ul>
    </section>
  );
}

function NewExtension({ onMade }) {
  const [name, setName] = useState("");
  const a = useAction();
  const submit = async (ev) => {
    ev.preventDefault();
    const r = await a.run(() => post("/extensions", { name }), (r) => `Extension ${r.ext} made.`);
    if (r) {
      setName("");
      onMade();
    }
  };
  return (
    <form className="inline-form" onSubmit={submit}>
      <input placeholder="New extension's name" required maxLength={40} value={name}
        onChange={(e) => setName(e.target.value)} />
      <Button busy={a.busy} type="submit">Add</Button>
      {a.show}
    </form>
  );
}

function ExtensionEditor({ ext, me, fwd, onChange }) {
  const admin = me.role === "admin";
  return (
    <div className="editor">
      <NameAndSwitch ext={ext} admin={admin} onChange={onChange} />
      <Forwarding ext={ext.ext} fwd={fwd} onChange={onChange} />
      <VoicemailEmail ext={ext} me={me} onChange={onChange} />
      <VoicemailAccess ext={ext} me={me} onChange={onChange} />
      <PhoneSetup ext={ext} me={me} />
      <NewSecrets ext={ext.ext} />
    </div>
  );
}

function NameAndSwitch({ ext, admin, onChange }) {
  const [name, setName] = useState(ext.name);
  const a = useAction();
  const save = (enabled) => a.run(() => put(`/extensions/${ext.ext}`, enabled === undefined ? { name } : { name, enabled }),
    "Saved.").then((r) => r && onChange());
  return (
    <div className="block">
      <h3>Name</h3>
      <div className="inline-form">
        <input maxLength={40} value={name} onChange={(e) => setName(e.target.value)} aria-label="Name" />
        <Button busy={a.busy} kind="secondary" onClick={() => save()} disabled={!name.trim() || name === ext.name}>Save</Button>
      </div>
      {admin && (
        <label className="check">
          <input type="checkbox" checked={ext.enabled} disabled={a.busy}
            onChange={(e) => window.confirm(e.target.checked ? `Switch extension ${ext.ext} on?`
              : `Switch extension ${ext.ext} off? Its phones stop working until it's switched on again.`) && save(e.target.checked)} />
          Switched on
        </label>
      )}
      {a.show}
    </div>
  );
}

function Forwarding({ ext, fwd, onChange }) {
  const blank = { dnd: false, always: "", busy: "", no_answer: "" };
  const [f, setF] = useState({ ...blank, ...(fwd || {}) });
  useEffect(() => { setF({ ...blank, ...(fwd || {}) }); }, [fwd]); // eslint-disable-line react-hooks/exhaustive-deps
  const a = useAction();
  const save = () => a.run(() => put(`/extensions/${ext}/forwarding`, f), "Saved.").then((r) => r && onChange());
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  return (
    <div className="block">
      <h3>Forwarding</h3>
      <label className="check"><input type="checkbox" checked={f.dnd} onChange={set("dnd")} /> Do not disturb: this phone doesn't ring</label>
      <Field label="Forward every call to" hint="Another extension or an Irish number. Empty: ring this phone.">
        <input inputMode="tel" value={f.always} onChange={set("always")} placeholder="e.g. 202 or 0871234567" />
      </Field>
      <Field label="When busy, forward to"><input inputMode="tel" value={f.busy} onChange={set("busy")} /></Field>
      <Field label="When not answered, forward to"><input inputMode="tel" value={f.no_answer} onChange={set("no_answer")} /></Field>
      <Button busy={a.busy} kind="secondary" onClick={save}>Save forwarding</Button>
      {a.show}
    </div>
  );
}

function VoicemailEmail({ ext, me, onChange }) {
  const admin = me.role === "admin";
  const on = !!ext.vm_email;
  const [email, setEmail] = useState(ext.vm_email || "");
  const a = useAction();
  const save = (value) => a.run(() => put(`/extensions/${ext.ext}/voicemail-email`, { email: value }), "Saved.")
    .then((r) => r && onChange());
  return (
    <div className="block">
      <h3>Voicemail by email</h3>
      {admin ? (
        <div className="inline-form">
          <input type="email" placeholder="Email new messages to…" value={email} onChange={(e) => setEmail(e.target.value)} />
          <Button busy={a.busy} kind="secondary" onClick={() => save(email)}>Save</Button>
        </div>
      ) : (
        <label className="check">
          <input type="checkbox" checked={on} disabled={a.busy} onChange={(e) => save(e.target.checked ? me.email : "")} />
          Email my new voicemail to {me.email}
        </label>
      )}
      {a.show}
    </div>
  );
}

function VoicemailAccess({ ext, me, onChange }) {
  const own = me.role !== "admin";
  const [pin, setPin] = useState("");
  const [again, setAgain] = useState("");
  const a = useAction();
  const b = useAction();
  const toggle = (on) => a.run(() => put(`/extensions/${ext.ext}/voicemail-nopin`, { on }),
    on ? "Done: *97 from this extension's phones goes straight in." : "Done: *97 asks for the PIN again.")
    .then((r) => r && onChange());
  const choose = async (e) => {
    e.preventDefault();
    if (pin !== again) {
      b.setMsg({ kind: "error", text: "The two PINs aren't the same." });
      return;
    }
    const r = await b.run(() => put(`/extensions/${ext.ext}/voicemail-pin`, { pin }), "PIN saved.");
    if (r) {
      setPin("");
      setAgain("");
    }
  };
  return (
    <div className="block">
      <h3>Voicemail access</h3>
      <label className="check">
        <input type="checkbox" checked={!!ext.vm_nopin} disabled={a.busy} onChange={(e) => toggle(e.target.checked)} />
        Listen without the PIN from {own ? "my" : "this extension's"} own phones (dial *97)
      </label>
      <p className="small muted">Safe because those phones have already signed in with the extension&rsquo;s password.
        From anywhere else the PIN is still asked. Anyone who picks up {own ? "your" : "its"} unlocked phone could
        then hear the messages.</p>
      {a.show}
      <form className="block" onSubmit={choose}>
        <Field label={own ? "Choose my PIN" : "Choose its PIN"} hint="4 to 8 digits; not 1234, repeats like 1111, or the extension number.">
          <input type="password" inputMode="numeric" autoComplete="new-password" pattern="[0-9]{4,8}" maxLength={8}
            value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} aria-label="New PIN" />
        </Field>
        <Field label="The same again">
          <input type="password" inputMode="numeric" autoComplete="new-password" pattern="[0-9]{4,8}" maxLength={8}
            value={again} onChange={(e) => setAgain(e.target.value.replace(/\D/g, ""))} aria-label="PIN again" />
        </Field>
        <Button busy={b.busy} kind="secondary" type="submit" disabled={pin.length < 4}>Save PIN</Button>
        {b.show}
      </form>
    </div>
  );
}

function PhoneSetup({ ext, me }) {
  const admin = me.role === "admin";
  const [app, setApp] = useState("groundwire");
  const [link, setLink] = useState(null);
  const [to, setTo] = useState(admin ? "" : me.email);
  const a = useAction();
  const b = useAction();
  if (!ext.enabled) return null;
  return (
    <div className="block">
      <h3>Set up a phone</h3>
      <p className="small">The app fills in every setting itself. Groundwire on iPhone and Android, or Linphone.</p>
      <div className="inline-form">
        <select value={app} onChange={(e) => setApp(e.target.value)} aria-label="App">
          <option value="groundwire">Groundwire</option>
          <option value="linphone">Linphone</option>
        </select>
        <Button busy={a.busy} kind="secondary" onClick={() => a.run(() => post(`/extensions/${ext.ext}/setup-link`, { app }))
          .then((r) => r && setLink(r))}>Show a QR code</Button>
      </div>
      {link && (
        <div className="qr-box">
          <img className="qr" src={link.qr} alt="Setup QR code" />
          <p className="small">Scan it with the phone's camera ({app === "linphone" ? "from inside Linphone" : "it opens Groundwire"}).
            It works once, for 15 minutes. On the phone itself? <a href={link.url}>Open the setup link</a>.</p>
        </div>
      )}
      {a.show}
      <div className="inline-form">
        {admin ? <input type="email" placeholder="Email the setup to…" value={to} onChange={(e) => setTo(e.target.value)} />
          : <span className="grow small">Or email it to {me.email}, with your voicemail PIN.</span>}
        <Button busy={b.busy} kind="secondary" disabled={!to}
          onClick={() => b.run(() => post(`/extensions/${ext.ext}/setup-email`, { apps: [app], to }), (r) => `Sent to ${r.to}.`)}>
          Email it</Button>
      </div>
      {b.show}
    </div>
  );
}

function NewSecrets({ ext }) {
  const a = useAction();
  return (
    <div className="block">
      <h3>Passwords</h3>
      <div className="buttons">
        <Button busy={a.busy} kind="secondary" onClick={() => window.confirm(
          "Make a new voicemail PIN? The old one stops working. It's sent in the next setup email.")
          && a.run(() => post(`/extensions/${ext}/new-pin`), (r) => r.message)}>New voicemail PIN</Button>
        <Button busy={a.busy} kind="danger" onClick={() => window.confirm(
          `Make a new phone password for ${ext}? Every phone on it stops working until it's set up again with a QR code or email.`)
          && a.run(() => post(`/extensions/${ext}/new-password`), (r) => r.message)}>New phone password</Button>
      </div>
      {a.show}
    </div>
  );
}

// -- Status ------------------------------------------------------------------------------------

export function Status() {
  const [s, setS] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () => get("/status").then((r) => live && (setS(r), setError(null))).catch((e) => live && setError(e.message));
    load();
    const t = setInterval(load, 10000);
    return () => { live = false; clearInterval(t); };
  }, []);
  if (error) return <Message kind="error">{error}</Message>;
  if (!s) return <p className="muted">Loading…</p>;
  const phones = Object.entries(s.phones);
  return (
    <section>
      <h2>Status</h2>
      {!s.running && <Message kind="error">The phone system isn't answering right now.</Message>}
      <h3>Phones</h3>
      {phones.length === 0 ? <p className="muted">No phones connected.</p> : (
        <ul className="list compact">
          {phones.map(([ext, p]) => (
            <li key={ext} className="row-head static">
              <span className={`dot ${p.status === "Avail" ? "dot-ok" : "dot-off"}`} />
              <span className="ext">{ext}</span>
              <span className="grow">{p.status === "Avail" ? "Connected" : "Not connected"}</span>
            </li>
          ))}
        </ul>
      )}
      <h3>Calls now</h3>
      {s.calls.length === 0 ? <p className="muted">No calls right now.</p> : (
        <ul className="list compact">
          {s.calls.map((c, i) => (
            <li key={i} className="row-head static">
              <span className="ext">{c.ext}</span>
              <span className="grow">{c.state === "Up" ? "On a call" : c.state === "Ring" || c.state === "Ringing" ? "Ringing" : c.state}
                {c.caller_id ? ` · ${c.caller_id}` : ""}</span>
              <span className="muted">{Math.floor(c.duration_seconds / 60)}:{String(c.duration_seconds % 60).padStart(2, "0")}</span>
              {c.recording && <span className="tag tag-rec">Recording</span>}
            </li>
          ))}
        </ul>
      )}
      <p className="small muted">Updates every 10 seconds.</p>
    </section>
  );
}

// -- Recordings ------------------------------------------------------------------------------------

const DIRECTIONS = { in: "Call in", out: "Call out", int: "Internal", req: "Recorded on request" };

function when(started) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(started || "");
  return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : started;
}

export function Recordings({ me }) {
  const [list, setList] = useState(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState(null);
  const [playing, setPlaying] = useState(null);
  const a = useAction();
  const load = useCallback(() => get(`/recordings${q ? `?q=${encodeURIComponent(q)}` : ""}`)
    .then((r) => { setList(r.recordings); setError(null); }).catch((e) => setError(e.message)), [q]);
  useEffect(() => { load(); }, [load]);

  const remove = async (name) => {
    try {
      await api("DELETE", `/recordings/${encodeURIComponent(name)}`);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409 && e.data?.confirm_needed)) {
        a.setMsg({ kind: "error", text: e.message });
        return;
      }
      if (!window.confirm(e.message)) return; // the API's own warning
      const r = await a.run(() => api("DELETE", `/recordings/${encodeURIComponent(name)}?confirm=true`), "Deleted.");
      if (r) load();
    }
  };
  if (error) return <Message kind="error">{error}</Message>;
  return (
    <section>
      <h2>Recordings</h2>
      <input className="search" inputMode="tel" placeholder="Search by number" value={q} onChange={(e) => setQ(e.target.value)} />
      {a.show}
      {!list ? <p className="muted">Loading…</p> : list.length === 0 ? <p className="muted">No recordings.</p> : (
        <ul className="list">
          {list.map((r) => (
            <li key={r.name} className="row">
              <div className="row-head static">
                <span className="ext">{r.ext}</span>
                <span className="grow">{DIRECTIONS[r.direction] || r.direction}{r.other && r.other !== "unknown" ? ` · ${r.other}` : ""}
                  <span className="small muted block">{when(r.started)} · {Math.round(r.seconds / 60) || "<1"} min</span></span>
                <button className="btn btn-secondary" onClick={() => setPlaying(playing === r.name ? null : r.name)}>
                  {playing === r.name ? "Close" : "Play"}</button>
              </div>
              {playing === r.name && (
                <div className="player">
                  <audio controls autoPlay preload="none" src={`/api/recordings/${encodeURIComponent(r.name)}`} />
                  <div className="buttons">
                    <a className="btn btn-secondary" href={`/api/recordings/${encodeURIComponent(r.name)}?download=true`}>Download</a>
                    {me.role === "admin" && <Button kind="danger" onClick={() => remove(r.name)}>Delete…</Button>}
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
