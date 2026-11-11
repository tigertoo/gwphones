import React, { useCallback, useEffect, useState } from "react";
import { ApiError, del, get, post, put } from "./api";
import { playMulaw, startRecording, toMulaw } from "./ivrAudio";
import { Button, Field, Message, useAction } from "./ui";

// Call routing, for admins: opening hours (with "closed today" at the top
// of each), IVR menus with their greetings, ring groups, and where the
// organisation's numbers go (read only: we set those). Destinations come
// from the API with their labels.

const DAYS = [["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"], ["fri", "Friday"],
  ["sat", "Saturday"], ["sun", "Sunday"]];
const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "*", "#"];
const KINDS = [["extension", "Ring an extension"], ["group", "Ring group"], ["menu", "Menu"], ["hours", "Opening hours"],
  ["voicemail", "Voicemail"], ["hangup", "Other"]];

export function Routing() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [part, setPart] = useState("hours");
  const load = useCallback(() => get("/routing").then((r) => { setData(r); setError(null); }).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  if (error) return <Message kind="error">{error}</Message>;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <section>
      <h2>Call routing</h2>
      <div className="seg" role="tablist">
        {[["hours", "Opening hours"], ["menus", "Menus"], ["groups", "Ring groups"], ["numbers", "Numbers"],
          ["links", "Call links"]].map(([k, label]) => (
          <button key={k} role="tab" aria-selected={part === k} className={`seg-btn ${part === k ? "seg-on" : ""}`}
            onClick={() => setPart(k)}>{label}</button>
        ))}
      </div>
      {part === "hours" && <HoursList data={data} onChange={load} />}
      {part === "menus" && <Menus data={data} onChange={load} />}
      {part === "groups" && <Groups data={data} onChange={load} />}
      {part === "numbers" && <Numbers data={data} onChange={load} />}
      {part === "links" && <CallLinks data={data} />}
    </section>
  );
}

export function describe(data, value) {
  if (!value) return "nowhere";
  return data.destinations.find((d) => d.value === value)?.label || value;
}

function Destination({ data, value, onChange, empty = "Choose…", exclude = [], self, label }) {
  return (
    <select value={value || ""} onChange={(e) => onChange(e.target.value)} aria-label={label}>
      <option value="">{empty}</option>
      {KINDS.filter(([k]) => !exclude.includes(k)).map(([kind, heading]) => {
        const items = data.destinations.filter((d) => d.kind === kind);
        if (!items.length) return null;
        return (
          <optgroup key={kind} label={heading}>
            {items.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}{d.value === self ? " (this one again)" : ""}{d.enabled ? "" : " (switched off)"}
              </option>
            ))}
          </optgroup>
        );
      })}
    </select>
  );
}

// -- opening hours ------------------------------------------------------------------------------

function HoursList({ data, onChange }) {
  const [making, setMaking] = useState(false);
  return (
    <>
      <p className="small muted">Send calls one way while you're open and another when you're closed. A number, a menu
        key or a call link can point at opening hours.</p>
      <ul className="list">
        {data.business_hours.map((h) => <HoursItem key={h.number} h={h} data={data} onChange={onChange} />)}
      </ul>
      {making ? <HoursEditor data={data} onDone={() => { setMaking(false); onChange(); }} onCancel={() => setMaking(false)} />
        : <Button kind="secondary" onClick={() => setMaking(true)}>New opening hours</Button>}
    </>
  );
}

function HoursItem({ h, data, onChange }) {
  const [open, setOpen] = useState(false);
  const a = useAction();
  const override = (value) => a.run(() => put(`/hours/${h.number}/override`, { override: value }),
    value === "closed" ? "Closed until you switch it back." : value === "open" ? "Open until you switch it back." : "Back to the hours.")
    .then((r) => r && onChange());
  const state = h.override === "closed" ? "Closed (switched)" : h.override === "open" ? "Open (switched)" : h.open_now ? "Open now" : "Closed now";
  return (
    <li className={`row ${open ? "row-open" : ""}`}>
      <div className="row-head static">
        <span className="ext">{h.number}</span>
        <span className="grow">{h.name}<span className="small muted block">{state}</span></span>
        <span className={`tag ${h.open_now && h.override !== "closed" || h.override === "open" ? "" : "tag-off"}`}>
          {h.override ? "Switched" : h.open_now ? "Open" : "Closed"}</span>
      </div>
      <div className="editor">
        <div className="buttons">
          {h.override !== "closed" && <Button kind="secondary" busy={a.busy} onClick={() => override("closed")}>Close now</Button>}
          {h.override !== "open" && <Button kind="secondary" busy={a.busy} onClick={() => override("open")}>Open now</Button>}
          {h.override && <Button kind="secondary" busy={a.busy} onClick={() => override("")}>Back to the hours</Button>}
          <Button kind="secondary" onClick={() => setOpen(!open)}>{open ? "Close" : "Change"}</Button>
        </div>
        {a.show}
        <p className="small muted">Open: to {describe(data, h.open_target)}. Closed: to {describe(data, h.closed_target)}.</p>
        {open && <HoursEditor data={data} h={h} onDone={() => { setOpen(false); onChange(); }} onCancel={() => setOpen(false)} />}
      </div>
    </li>
  );
}

function HoursEditor({ data, h, onDone, onCancel }) {
  const [name, setName] = useState(h?.name || "");
  const [week, setWeek] = useState(() => Object.fromEntries(DAYS.map(([d]) => [d, h?.week?.[d] || []])));
  const [holidays, setHolidays] = useState(h?.holidays || []);
  const [newDay, setNewDay] = useState("");
  const [openTo, setOpenTo] = useState(h?.open_target || "");
  const [closedTo, setClosedTo] = useState(h?.closed_target || "");
  const a = useAction();
  const setRange = (day, i, j, v) => setWeek({ ...week, [day]: week[day].map((r, k) => (k === i ? (j ? [r[0], v] : [v, r[1]]) : r)) });
  const save = async () => {
    const body = { name, week, holidays, open_target: openTo, closed_target: closedTo, override: h?.override || "" };
    const r = await a.run(() => (h ? put(`/hours/${h.number}`, body) : post("/hours", body)), "Saved.");
    if (r) onDone();
  };
  const remove = async () => {
    if (!window.confirm(`Delete opening hours ${h.number} (${h.name})?`)) return;
    const r = await a.run(() => del(`/hours/${h.number}`), "Deleted.");
    if (r) onDone();
  };
  return (
    <div className="card">
      <Field label="Name"><input maxLength={40} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Office hours" /></Field>
      <div className="block">
        <h3>Open</h3>
        {DAYS.map(([d, label]) => (
          <div key={d} className="day">
            <span className="day-name">{label}</span>
            <div className="day-ranges">
              {week[d].length === 0 && <span className="small muted">Closed</span>}
              {week[d].map((r, i) => (
                <div key={i} className="range">
                  <input type="time" value={r[0]} onChange={(e) => setRange(d, i, 0, e.target.value)} aria-label={`${label} opens`} />
                  <span>–</span>
                  <input type="time" value={r[1]} onChange={(e) => setRange(d, i, 1, e.target.value)} aria-label={`${label} closes`} />
                  <button type="button" className="link" onClick={() => setWeek({ ...week, [d]: week[d].filter((_, k) => k !== i) })}
                    aria-label="Remove">✕</button>
                </div>
              ))}
              {week[d].length < 4 && (
                <button type="button" className="link" onClick={() => setWeek({ ...week, [d]: [...week[d], ["09:00", "17:00"]] })}>
                  + Add opening</button>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="block">
        <h3>Closed on these days</h3>
        {holidays.length === 0 && <p className="small muted">None.</p>}
        <div className="chips">
          {holidays.map((d) => (
            <span key={d} className="tag">{d} <button type="button" className="chip-x" aria-label={`Remove ${d}`}
              onClick={() => setHolidays(holidays.filter((x) => x !== d))}>✕</button></span>
          ))}
        </div>
        <div className="inline-form">
          <input type="date" value={newDay} onChange={(e) => setNewDay(e.target.value)} aria-label="A closed day" />
          <Button kind="secondary" disabled={!newDay} onClick={() => {
            if (!holidays.includes(newDay)) setHolidays([...holidays, newDay].sort());
            setNewDay("");
          }}>Add</Button>
        </div>
      </div>
      <Field label="While open, calls go to">
        <Destination data={data} value={openTo} onChange={setOpenTo} exclude={["hours"]} label="While open" />
      </Field>
      <Field label="While closed, calls go to">
        <Destination data={data} value={closedTo} onChange={setClosedTo} exclude={["hours"]} label="While closed" />
      </Field>
      {a.show}
      <div className="buttons">
        <Button busy={a.busy} disabled={!name.trim() || !openTo || !closedTo} onClick={save}>Save</Button>
        <Button kind="secondary" onClick={onCancel}>Cancel</Button>
        {h && <Button kind="danger" busy={a.busy} onClick={remove}>Delete</Button>}
      </div>
    </div>
  );
}

// -- menus --------------------------------------------------------------------------------------

function Menus({ data, onChange }) {
  const [making, setMaking] = useState(false);
  return (
    <>
      <p className="small muted">"Press 1 for sales, 2 for accounts…": a greeting, then each key sends the call somewhere.</p>
      <ul className="list">
        {data.ivr_menus.map((m) => <MenuItem key={m.number} m={m} data={data} onChange={onChange} />)}
      </ul>
      {making ? <MenuEditor data={data} onDone={() => { setMaking(false); onChange(); }} onCancel={() => setMaking(false)} />
        : <Button kind="secondary" onClick={() => setMaking(true)}>New menu</Button>}
    </>
  );
}

function MenuItem({ m, data, onChange }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`row ${open ? "row-open" : ""}`}>
      <button className="row-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="ext">{m.number}</span>
        <span className="grow">{m.name}<span className="small muted block">
          {Object.keys(m.options).sort().map((k) => `${k}: ${describe(data, m.options[k]).replace(/^\d+ · /, "")}`).join(" · ")}</span></span>
        {!m.greeting && <span className="tag tag-off">No greeting</span>}
      </button>
      {open && (
        <div className="editor">
          <Greeting m={m} onChange={onChange} />
          <MenuEditor data={data} m={m} onDone={() => { setOpen(false); onChange(); }} onCancel={() => setOpen(false)} />
        </div>
      )}
    </li>
  );
}

function Greeting({ m, onChange }) {
  const [stop, setStop] = useState(null);
  const [rec, setRec] = useState(null);
  const a = useAction();
  const upload = (bytes) => a.run(() => fetch(`/api/menus/${m.number}/greeting`, {
    method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/octet-stream" }, body: bytes,
  }).then(async (res) => {
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(d.detail || "The greeting couldn't be saved.", res.status, d);
    return d;
  }), "Greeting saved.").then((r) => r && onChange());
  const fromFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      upload(await toMulaw(await file.arrayBuffer()));
    } catch (err) {
      a.setMsg({ kind: "error", text: err.message });
    }
  };
  const play = async () => {
    if (stop) { stop(); setStop(null); return; }
    try {
      const res = await fetch(`/api/menus/${m.number}/greeting`, { credentials: "same-origin" });
      if (!res.ok) throw new Error("No greeting to play.");
      const bytes = new Uint8Array(await res.arrayBuffer());
      const stopper = playMulaw(bytes, () => setStop(null));
      setStop(() => stopper);
    } catch (err) {
      a.setMsg({ kind: "error", text: err.message });
    }
  };
  const record = async () => {
    if (rec) {
      try {
        upload(await rec());
      } catch (err) {
        a.setMsg({ kind: "error", text: err.message });
      }
      setRec(null);
      return;
    }
    try {
      const stopper = await startRecording();
      setRec(() => stopper);
    } catch {
      a.setMsg({ kind: "error", text: "The microphone isn't available (allow it for this site)." });
    }
  };
  return (
    <div className="block">
      <h3>Greeting</h3>
      <p className="small muted">{m.greeting ? "Callers hear it, then press a key." : "None yet: callers hear silence until a key."} Up
        to 2 minutes: record it here, or choose an audio file (MP3, WAV, M4A…).</p>
      <div className="buttons">
        {m.greeting && <Button kind="secondary" onClick={play}>{stop ? "Stop" : "Play"}</Button>}
        <Button kind={rec ? "primary" : "secondary"} busy={a.busy && !rec} onClick={record}>{rec ? "Stop and save" : "Record"}</Button>
        <label className="btn btn-secondary file-btn">Choose a file
          <input type="file" accept="audio/*" onChange={fromFile} hidden />
        </label>
        {m.greeting && <Button kind="danger" busy={a.busy} onClick={() => window.confirm("Remove the greeting?")
          && a.run(() => del(`/menus/${m.number}/greeting`), "Greeting removed.").then((r) => r && onChange())}>Remove</Button>}
      </div>
      {rec && <Message kind="info">Recording… speak now, then press Stop and save.</Message>}
      {a.show}
    </div>
  );
}

function MenuEditor({ data, m, onDone, onCancel }) {
  const [name, setName] = useState(m?.name || "");
  const [options, setOptions] = useState(m?.options || {});
  const [fallback, setFallback] = useState(m?.fallback || "");
  const [timeout, setTimeoutS] = useState(m?.timeout || 6);
  const [retries, setRetries] = useState(m?.retries || 3);
  const [direct, setDirect] = useState(!!m?.direct_dial);
  const a = useAction();
  const setKey = (k, v) => {
    const next = { ...options };
    if (v) next[k] = v; else delete next[k];
    setOptions(next);
  };
  const save = async () => {
    const body = { name, options, fallback, timeout: Number(timeout), retries: Number(retries), direct_dial: direct };
    const r = await a.run(() => (m ? put(`/menus/${m.number}`, body) : post("/menus", body)), "Saved.");
    if (r) onDone();
  };
  const remove = async () => {
    if (!window.confirm(`Delete menu ${m.number} (${m.name}) and its greeting?`)) return;
    const r = await a.run(() => del(`/menus/${m.number}`), "Deleted.");
    if (r) onDone();
  };
  return (
    <div className="card">
      <Field label="Name"><input maxLength={40} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Main menu" /></Field>
      <div className="block">
        <h3>Keys</h3>
        {KEYS.map((k) => (
          <div key={k} className="key-row">
            <span className="key">{k}</span>
            <Destination data={data} value={options[k]} onChange={(v) => setKey(k, v)} empty="Not used" self={m?.number}
              label={`Key ${k}`} />
          </div>
        ))}
      </div>
      <Field label="If nothing is pressed, in the end" hint="After the greeting has played the number of times below.">
        <Destination data={data} value={fallback} onChange={setFallback} empty="Say goodbye" self={m?.number} label="In the end" />
      </Field>
      <div className="two">
        <Field label="Wait for a key (seconds)"><input type="number" min={2} max={30} value={timeout} onChange={(e) => setTimeoutS(e.target.value)} /></Field>
        <Field label="Play the greeting (times)"><input type="number" min={1} max={5} value={retries} onChange={(e) => setRetries(e.target.value)} /></Field>
      </div>
      <label className="check"><input type="checkbox" checked={direct} onChange={(e) => setDirect(e.target.checked)} />
        Callers may also dial an extension number directly</label>
      {a.show}
      <div className="buttons">
        <Button busy={a.busy} disabled={!name.trim() || !Object.keys(options).length} onClick={save}>Save</Button>
        <Button kind="secondary" onClick={onCancel}>Cancel</Button>
        {m && <Button kind="danger" busy={a.busy} onClick={remove}>Delete</Button>}
      </div>
    </div>
  );
}

// -- ring groups ------------------------------------------------------------------------------------

function Groups({ data, onChange }) {
  const [making, setMaking] = useState(false);
  return (
    <>
      <p className="small muted">Several phones ringing for one number: all at once, or one after another.</p>
      <ul className="list">
        {data.ring_groups.map((g) => <GroupItem key={g.number} g={g} data={data} onChange={onChange} />)}
      </ul>
      {making ? <GroupEditor data={data} onDone={() => { setMaking(false); onChange(); }} onCancel={() => setMaking(false)} />
        : <Button kind="secondary" onClick={() => setMaking(true)}>New ring group</Button>}
    </>
  );
}

function GroupItem({ g, data, onChange }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`row ${open ? "row-open" : ""}`}>
      <button className="row-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="ext">{g.number}</span>
        <span className="grow">{g.name}<span className="small muted block">
          {g.strategy === "hunt" ? "One after another" : "All at once"}: {g.members.join(", ")}</span></span>
      </button>
      {open && <div className="editor"><GroupEditor data={data} g={g} onDone={() => { setOpen(false); onChange(); }}
        onCancel={() => setOpen(false)} /></div>}
    </li>
  );
}

function GroupEditor({ data, g, onDone, onCancel }) {
  const exts = data.destinations.filter((d) => d.kind === "extension");
  const [name, setName] = useState(g?.name || "");
  const [members, setMembers] = useState(g?.members || []);
  const [strategy, setStrategy] = useState(g?.strategy || "ringall");
  const [ringTime, setRingTime] = useState(g?.ring_time || 25);
  const [voicemail, setVoicemail] = useState(g?.voicemail || "");
  const a = useAction();
  const toggle = (ext) => setMembers(members.includes(ext) ? members.filter((m) => m !== ext) : [...members, ext]);
  const save = async () => {
    const body = { name, members, strategy, ring_time: Number(ringTime), voicemail };
    const r = await a.run(() => (g ? put(`/groups/${g.number}`, body) : post("/groups", body)), "Saved.");
    if (r) onDone();
  };
  const remove = async () => {
    if (!window.confirm(`Delete ring group ${g.number} (${g.name})?`)) return;
    const r = await a.run(() => del(`/groups/${g.number}`), "Deleted.");
    if (r) onDone();
  };
  return (
    <div className="card">
      <Field label="Name"><input maxLength={40} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Sales" /></Field>
      <div className="block">
        <h3>Who rings</h3>
        {exts.map((e) => (
          <label key={e.value} className="check">
            <input type="checkbox" checked={members.includes(e.value)} onChange={() => toggle(e.value)} /> {e.label}
          </label>
        ))}
      </div>
      <Field label="How">
        <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
          <option value="ringall">All at once</option>
          <option value="hunt">One after another, in the order ticked</option>
        </select>
      </Field>
      <Field label="Ring for (seconds)"><input type="number" min={5} max={120} value={ringTime} onChange={(e) => setRingTime(e.target.value)} /></Field>
      <Field label="If nobody answers, leave a voicemail for">
        <select value={voicemail} onChange={(e) => setVoicemail(e.target.value)}>
          <option value="">Nobody: the caller hears it ring out</option>
          {exts.map((e) => <option key={e.value} value={e.value}>{e.label}</option>)}
        </select>
      </Field>
      {a.show}
      <div className="buttons">
        <Button busy={a.busy} disabled={!name.trim() || !members.length} onClick={save}>Save</Button>
        <Button kind="secondary" onClick={onCancel}>Cancel</Button>
        {g && <Button kind="danger" busy={a.busy} onClick={remove}>Delete</Button>}
      </div>
    </div>
  );
}

// -- numbers (read only) --------------------------------------------------------------------------------

function Numbers({ data, onChange }) {
  return (
    <>
      <p className="small muted">Your phone numbers and where their calls go: an extension, a ring group, a menu or
        opening hours. To add or give up a number, contact us.</p>
      {data.numbers.length === 0 ? <p className="muted">No numbers yet.</p> : (
        <ul className="list">
          {data.numbers.map((n) => <NumberItem key={n.did} n={n} data={data} onChange={onChange} />)}
        </ul>
      )}
    </>
  );
}

function NumberItem({ n, data, onChange }) {
  const [target, setTarget] = useState(n.target);
  const a = useAction();
  const save = () => a.run(() => put(`/numbers/${n.did}`, { target }), "Saved.").then((r) => r && onChange());
  return (
    <li className="row">
      <div className="row-head static">
        <span className="grow">{n.label || n.did}<span className="small muted block">{n.did}</span></span>
      </div>
      <div className="editor">
        <Field label="Calls to this number go to">
          <Destination data={data} value={target} onChange={setTarget} exclude={["voicemail", "hangup"]}
            label={`Calls to ${n.did}`} />
        </Field>
        <div className="buttons">
          <Button busy={a.busy} kind="secondary" disabled={!target || target === n.target} onClick={() => window.confirm(
            `Send every call to ${n.label || n.did} to ${describe(data, target)}? It takes effect straight away.`) && save()}>
            Save</Button>
        </div>
        {a.show}
      </div>
    </li>
  );
}

// -- call links ------------------------------------------------------------------------------------

function CallLinks({ data }) {
  const [links, setLinks] = useState(null);
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);
  const [making, setMaking] = useState(false);
  const load = useCallback(() => get("/call-links").then((r) => { setLinks(r.links); setInfo(r); setError(null); })
    .catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  if (error) return <Message kind="error">{error}</Message>;
  if (!links) return <p className="muted">Loading…</p>;
  return (
    <>
      <p className="small muted">A web address that calls you from the visitor's browser: put it on your website or in
        your email signature. Visitors need no phone and no app; they press Call.</p>
      {!info.ready && <Message kind="info">Call links aren't switched on for this system yet. Contact us.</Message>}
      <ul className="list">
        {links.map((l) => <LinkItem key={l.id} l={l} data={data} max={info.max_minutes} onChange={load} />)}
      </ul>
      {making ? <LinkEditor data={data} max={info.max_minutes} onDone={() => { setMaking(false); load(); }}
        onCancel={() => setMaking(false)} />
        : <Button kind="secondary" onClick={() => setMaking(true)}>New call link</Button>}
    </>
  );
}

function LinkItem({ l, data, max, onChange }) {
  const [open, setOpen] = useState(false);
  const [qr, setQr] = useState(false);
  const a = useAction();
  const copy = () => navigator.clipboard.writeText(l.url).then(() => a.setMsg({ kind: "ok", text: "Copied." }),
    () => a.setMsg({ kind: "error", text: "Couldn't copy: select the address and copy it instead." }));
  const working = l.enabled && l.live;
  const state = !l.enabled ? "Switched off" : l.live ? "Working" : "Not working: where it rings is switched off or gone";
  return (
    <li className={`row ${open ? "row-open" : ""}`}>
      <div className="row-head static">
        <span className="grow">{l.label}<span className="small muted block">Rings {describe(data, l.target)}
          {l.video ? " · with video" : ""}</span></span>
        <span className={`tag ${working ? "" : "tag-off"}`}>{working ? "Working" : "Off"}</span>
      </div>
      <div className="editor">
        <p className="small muted">{state}. Calls last at most {l.max_minutes} minutes.</p>
        <input readOnly value={l.url} aria-label="The link's address" onFocus={(e) => e.target.select()} />
        <div className="buttons">
          <Button kind="secondary" onClick={copy}>Copy</Button>
          <Button kind="secondary" onClick={() => setQr(!qr)}>{qr ? "Hide QR code" : "QR code"}</Button>
          <Button kind="secondary" onClick={() => setOpen(!open)}>{open ? "Close" : "Change"}</Button>
        </div>
        {qr && l.qr && <img className="qr" src={l.qr} alt="QR code for the call link" />}
        {a.show}
        {open && <LinkEditor data={data} l={l} max={max} onDone={() => { setOpen(false); onChange(); }}
          onCancel={() => setOpen(false)} />}
      </div>
    </li>
  );
}

function LinkEditor({ data, l, max, onDone, onCancel }) {
  const [label, setLabel] = useState(l?.label || "");
  const [target, setTarget] = useState(l?.target || "");
  const [minutes, setMinutes] = useState(l?.max_minutes || 30);
  const [enabled, setEnabled] = useState(l ? l.enabled : true);
  const a = useAction();
  const save = async () => {
    const body = { label, target, max_minutes: Number(minutes), enabled };
    const r = await a.run(() => (l ? put(`/call-links/${l.id}`, body) : post("/call-links", body)), "Saved.");
    if (r) onDone();
  };
  const newUrl = async () => {
    if (!window.confirm("Make a new address for this link? The old one stops working at once, wherever it's been shared.")) return;
    const r = await a.run(() => post(`/call-links/${l.id}/new-url`), "New address made.");
    if (r) onDone();
  };
  const remove = async () => {
    if (!window.confirm(`Delete the call link "${l.label}"? Its address stops working at once.`)) return;
    const r = await a.run(() => del(`/call-links/${l.id}`), "Deleted.");
    if (r) onDone();
  };
  return (
    <div className="card">
      <Field label="Name"><input maxLength={40} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Website" /></Field>
      <Field label="It rings">
        <Destination data={data} value={target} onChange={setTarget} exclude={["voicemail", "hangup"]} label="It rings" />
      </Field>
      <Field label="Longest call (minutes)">
        <input type="number" min={1} max={max} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
      </Field>
      <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Switched on</label>
      {a.show}
      <div className="buttons">
        <Button busy={a.busy} disabled={!label.trim() || !target} onClick={save}>Save</Button>
        <Button kind="secondary" onClick={onCancel}>Cancel</Button>
        {l && <Button kind="secondary" busy={a.busy} onClick={newUrl}>New address</Button>}
        {l && <Button kind="danger" busy={a.busy} onClick={remove}>Delete</Button>}
      </div>
    </div>
  );
}
