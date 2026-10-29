import React, { useEffect, useRef, useState } from "react";
import { MAX_SECONDS, playMulaw, startRecording, toMulaw } from "./ivrAudio";

// IVR menus ("press 1 for sales..."): see backend/ivr.py. Greetings are
// converted here, in the browser, before they're sent (see ivrAudio.js).

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "*", "#"];
const EMPTY_MENU = { number: "", name: "", options: {}, fallback: "", timeout: 6, retries: 3, direct_dial: false };

export function TargetSelect({ config, self, value, onChange, empty, schedules = true }) {
  return (
    <select className="idea-select pbx-select" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{empty}</option>
      <optgroup label="Ring an extension">
        {config.extensions.map((e) => (
          <option key={e.ext} value={e.ext}>{e.ext} · {e.name}{e.enabled ? "" : " (switched off)"}</option>
        ))}
      </optgroup>
      <optgroup label="Ring group">
        {config.ring_groups.map((g) => <option key={g.number} value={g.number}>{g.number} · {g.name}</option>)}
      </optgroup>
      <optgroup label="Another menu">
        {config.ivr_menus.map((m) => (
          <option key={m.number} value={m.number}>{m.number} · {m.name}{m.number === self ? " (this one again)" : ""}</option>
        ))}
      </optgroup>
      {schedules && (
        <optgroup label="Business hours">
          {config.business_hours.map((h) => <option key={h.number} value={h.number}>{h.number} · {h.name}</option>)}
        </optgroup>
      )}
      <optgroup label="Leave a voicemail">
        {config.extensions.map((e) => <option key={e.ext} value={`vm:${e.ext}`}>Voicemail {e.ext} · {e.name}</option>)}
      </optgroup>
      <option value="hangup">Hang up</option>
    </select>
  );
}

export function describeTarget(config, t) {
  if (!t) return "";
  if (t === "hangup") return "hang up";
  if (t.startsWith("vm:")) return `voicemail ${t.slice(3)}`;
  const name = config.extensions.find((e) => e.ext === t)?.name || config.ring_groups.find((g) => g.number === t)?.name
    || config.ivr_menus.find((m) => m.number === t)?.name || config.business_hours.find((h) => h.number === t)?.name;
  return name ? `${t} ${name}` : `${t} (gone)`;
}

function Greeting({ menu, send, busy, apiBase }) {
  const [recording, setRecording] = useState(null); // stop function while recording
  const [playing, setPlaying] = useState(null); // stop function while playing
  const [error, setError] = useState("");
  const fileRef = useRef(null);
  useEffect(() => () => playing?.(), [playing]);

  const upload = async (bytes) =>
    send("PUT", `/ivr/${menu.number}/greeting`, bytes, `Greeting saved (${Math.round(bytes.length / 8000)} seconds).`);

  const fromFile = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    setError("");
    try {
      await upload(await toMulaw(await file.arrayBuffer()));
    } catch (err) {
      setError(err.message);
    }
  };

  const record = async () => {
    setError("");
    if (recording) {
      setRecording(null);
      try {
        await upload(await recording());
      } catch (err) {
        setError(err.message);
      }
      return;
    }
    try {
      const stop = await startRecording();
      setRecording(() => stop);
    } catch {
      setRecording(null);
      setError("The browser wasn't allowed to use the microphone.");
    }
  };

  const play = async () => {
    if (playing) { playing(); setPlaying(null); return; }
    setError("");
    const res = await fetch(`${apiBase}/api/pbx/ivr/${menu.number}/greeting`);
    if (!res.ok) { setError("Couldn't load the greeting."); return; }
    const stop = playMulaw(new Uint8Array(await res.arrayBuffer()), () => setPlaying(null));
    setPlaying(() => stop);
  };

  return (
    <div className="pbx-gap">
      <div className="ticket-action-label">
        Greeting: {menu.greeting ? "set" : <span className="pbx-hint">none yet, callers hear a beep</span>}
      </div>
      <div className="pbx-actions pbx-greeting">
        {menu.greeting && <button className="ticket-action-btn" onClick={play}>{playing ? "Stop" : "Play"}</button>}
        <button className="ticket-action-btn" disabled={busy || !!recording} onClick={() => fileRef.current.click()}>Upload a file</button>
        <input ref={fileRef} type="file" accept="audio/*" hidden onChange={fromFile} />
        <button className={`ticket-action-btn ${recording ? "idea-delete-btn" : ""}`} disabled={busy} onClick={record}>
          {recording ? "Stop and save" : "Record"}
        </button>
        {menu.greeting && (
          <button className="ticket-action-btn idea-delete-btn" disabled={busy || !!recording}
            onClick={() => window.confirm(`Remove the greeting of menu ${menu.number}?`) &&
              send("DELETE", `/ivr/${menu.number}/greeting`, null, "Greeting removed.")}
          >Remove</button>
        )}
      </div>
      {recording && <p className="pbx-hint">Recording… say the whole greeting, then press Stop and save.</p>}
      {error && <div className="pbx-warning">{error}</div>}
      <p className="pbx-hint">
        Up to {MAX_SECONDS / 60} minutes, from any audio file your browser can play (WAV, MP3, M4A…) or recorded here.
        It&rsquo;s converted to phone quality before it&rsquo;s sent.
      </p>
    </div>
  );
}

export default function IvrSection({ config, send, busy, apiBase }) {
  const [form, setForm] = useState(null); // null | { ...fields, isNew }

  const save = async () => {
    const body = {
      name: form.name, options: form.options, fallback: form.fallback, timeout: Number(form.timeout),
      retries: Number(form.retries), direct_dial: form.direct_dial,
    };
    const ok = form.isNew
      ? await send("POST", "/ivr", { ...body, number: form.number.trim() }, "Menu created. Now add its greeting.")
      : await send("PUT", `/ivr/${form.number}`, body, "Menu saved.");
    if (ok) setForm(null);
  };
  const setKey = (k, v) => {
    const options = { ...form.options };
    if (v) options[k] = v; else delete options[k];
    setForm({ ...form, options });
  };

  return (
    <section className="panel">
      <h2>IVR menus</h2>
      <p className="pbx-hint pbx-intro">
        A greeting, then &ldquo;press 1 for sales, 2 for support&rdquo;. Point an inbound number or a call link at a menu;
        phones can dial its number too. If nobody presses a key, or presses one that isn&rsquo;t listed, the greeting plays
        again, then the call goes to the fallback.
      </p>

      {config.ivr_menus.length === 0 && !form && <p className="panel-empty">No menus yet.</p>}
      <ul className="idea-list">
        {config.ivr_menus.map((m) => (
          <li key={m.number} className="idea-item">
            <div className="idea-item-head">
              <span className="idea-priority idea-priority-medium">{m.number}</span>
              <span className="idea-title">{m.name}</span>
              <span className="pbx-hint">
                {m.greeting ? "" : "no greeting · "}waits {m.timeout}s · plays {m.retries}×
                {" · "}then {m.fallback ? describeTarget(config, m.fallback) : "goodbye"}
                {m.direct_dial ? " · extensions can be dialled" : ""}
              </span>
            </div>
            <div className="pbx-chips">
              {KEYS.filter((k) => m.options[k]).map((k) => (
                <span key={k} className="pbx-chip">{k} → {describeTarget(config, m.options[k])}</span>
              ))}
            </div>
            <Greeting menu={m} send={send} busy={busy} apiBase={apiBase} />
            <div className="idea-item-foot">
              <button className="ticket-action-btn" onClick={() => setForm({ ...m, isNew: false })}>Edit</button>
              <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                onClick={() => window.confirm(`Delete menu ${m.number} (${m.name}) and its greeting?`) &&
                  send("DELETE", `/ivr/${m.number}`, null, "Menu deleted.")}
              >Delete</button>
            </div>
          </li>
        ))}
      </ul>

      {form ? (
        <div className="idea-item idea-item-editing pbx-gap">
          <div className="idea-form-row">
            {form.isNew && (
              <input className="idea-input pbx-num-input" placeholder="Number" value={form.number}
                title={`Leave blank for the next free number (${config.limits.menu_min}-${config.limits.menu_max})`}
                onChange={(e) => setForm({ ...form, number: e.target.value })} />
            )}
            <input className="idea-input pbx-name-input" placeholder="Menu name, e.g. Main menu" maxLength={40}
              value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="ticket-action-label pbx-gap">Keys</div>
          <div className="pbx-key-grid">
            {KEYS.map((k) => (
              <label key={k} className="pbx-key-row">
                <span className="pbx-order-num">{k}</span>
                <TargetSelect config={config} self={form.number} value={form.options[k] || ""} empty="Not used"
                  onChange={(v) => setKey(k, v)} />
              </label>
            ))}
          </div>
          <div className="idea-form-row pbx-gap">
            <label className="idea-field">
              Wait for a key
              <input className="idea-input pbx-num-input" type="number" min={2} max={30} value={form.timeout}
                onChange={(e) => setForm({ ...form, timeout: e.target.value })} />
              s
            </label>
            <label className="idea-field">
              Play the greeting
              <input className="idea-input pbx-num-input" type="number" min={1} max={5} value={form.retries}
                onChange={(e) => setForm({ ...form, retries: e.target.value })} />
              times
            </label>
          </div>
          <label className="pbx-field pbx-gap">
            <span>Then</span>
            <TargetSelect config={config} self={form.number} value={form.fallback} empty="Say goodbye and hang up"
              onChange={(v) => setForm({ ...form, fallback: v })} />
          </label>
          <label className="pbx-toggle pbx-gap">
            <input type="checkbox" checked={form.direct_dial} onChange={(e) => setForm({ ...form, direct_dial: e.target.checked })} />
            Callers who know an extension can dial it (a key 2 then waits a few seconds in case more digits follow)
          </label>
          <div className="ticket-action-buttons">
            <button className="ticket-action-btn" onClick={() => setForm(null)}>Cancel</button>
            <button className="ticket-action-btn ticket-action-btn-primary"
              disabled={busy || !form.name.trim() || Object.keys(form.options).length === 0} onClick={save}>
              {form.isNew ? "Create menu" : "Save menu"}
            </button>
          </div>
          {form.isNew && <p className="pbx-hint">You can add the greeting once the menu is created.</p>}
        </div>
      ) : (
        <button className="ticket-action-btn ticket-action-btn-primary pbx-gap" onClick={() => setForm({ ...EMPTY_MENU, isNew: true })}>
          New menu
        </button>
      )}
    </section>
  );
}
