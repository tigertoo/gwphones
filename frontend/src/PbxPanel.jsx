import React, { useEffect, useState, useCallback } from "react";
import BillingPanel from "./BillingPanel";
import TenantsSection from "./TenantsSection";
import { chooseTenant, forgetTenant, setShownTenant, tenantHeaders } from "./pbxTenant";
import IvrSection from "./IvrSection";
import HoursSection from "./HoursSection";
import RecordingsSection from "./RecordingsSection";
import SaveToast, { busyText } from "./SaveToast";

// Phone System tab: manages the Asterisk PBX on this VPS through the
// dashboard's own /api/pbx/* endpoints (see backend/pbx.py). Every save is
// applied to Asterisk straight away; calls in progress are not dropped.

const SECTIONS = [
  { key: "status", label: "Status" },
  { key: "call", label: "Click to Call" },
  { key: "links", label: "Call Links" },
  { key: "extensions", label: "Extensions" },
  { key: "trunks", label: "Trunks & Outbound" },
  { key: "inbound", label: "Inbound & Ring Groups" },
  { key: "ivr", label: "IVR Menus" },
  { key: "hours", label: "Business Hours" },
  { key: "recordings", label: "Recordings" },
  { key: "billing", label: "Billing" },
  { key: "tenants", label: "Tenants" },
];
const STATUS_POLL_MS = 5000;

function statusLevel(status) {
  if (status === "Avail" || status === "Reachable" || status === "NonQual") return "ok";
  if (status === "Unavail" || status === "Unreachable") return "critical";
  return "warning";
}

function StatusDot({ status, title }) {
  return <span className={`vital-dot vital-dot-${statusLevel(status)} pbx-dot`} title={title || status} />;
}

function formatDuration(s) {
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

function Result({ result }) {
  if (!result) return null;
  return (
    <div className={`ticket-action-result ticket-action-result-${result.ok ? "success" : "error"} pbx-result`}>
      {result.text}
    </div>
  );
}

export default function PbxPanel({ apiBase, callNumber }) {
  const [section, setSection] = useState(callNumber ? "call" : "status");
  useEffect(() => { if (callNumber) setSection("call"); }, [callNumber]);
  const [config, setConfig] = useState(null);
  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [result, setResult] = useState(null); // { ok, text } from the last save
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null); // see SaveToast
  const closeToast = useCallback(() => setToast(null), []);

  const loadConfig = useCallback(async () => {
    try {
      let res = await fetch(`${apiBase}/api/pbx/config`);
      if (res.status === 400) {
        // The tenant chosen is gone: back to the main tenant.
        forgetTenant();
        res = await fetch(`${apiBase}/api/pbx/config`);
      }
      if (!res.ok) throw new Error();
      const data = await res.json();
      setShownTenant(data.tenant.id); // what every save from here says it's for
      setConfig(data);
      setLoadError(null);
    } catch {
      setLoadError("Couldn't load the phone system settings. Check the dashboard service is running.");
    }
  }, [apiBase]);

  const loadStatus = useCallback(async () => {
    try {
      const res = await fetch(`${apiBase}/api/pbx/status`);
      if (res.ok) setStatus(await res.json());
    } catch {
      /* status is best-effort; the config error above covers a dead API */
    }
  }, [apiBase]);

  useEffect(() => {
    loadConfig();
    loadStatus();
    const t = setInterval(loadStatus, STATUS_POLL_MS);
    return () => clearInterval(t);
  }, [loadConfig, loadStatus]);

  // Every change goes through here: send, show the apply result, reload.
  const send = async (method, path, body, successText) => {
    setBusy(true);
    // Every change here is applied to Asterisk straight away (a reload).
    setToast({ busy: true, text: busyText(method, path, [
      [/reset-password$/, "Setting a new password and applying it…"],
      [/reset-voicemail-pin$/, "Setting a new voicemail PIN…"],
      [/^\/apply$/, "Applying to the phone system…"],
      [/^\/click-to-call$/, "Ringing your extension…"],
      [/^\/call-links/, method === "DELETE" ? "Deleting the link…" : "Saving the link and applying…"],
      [/\/greeting$/, method === "DELETE" ? "Removing the greeting…" : "Sending the greeting…"],
      [/\/override$/, "Switching and applying…"],
      [/^\/recordings\/start$/, "Starting the recording…"],
      [/^\/recordings\/file\//, "Deleting the recording…"],
      [/\/recording$/, "Saving and applying…"],
    ]).replace(/^Saving…$/, "Saving and applying to the phone system…").replace(/^Deleting…$/, "Deleting and applying…") });
    try {
      // Bytes (an IVR greeting) go as they are, anything else as JSON.
      const raw = body instanceof Uint8Array;
      const res = await fetch(`${apiBase}/api/pbx${path}`, {
        method,
        headers: { "Content-Type": raw ? "application/octet-stream" : "application/json", ...tenantHeaders() },
        body: raw ? body : body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const text = typeof data.detail === "string" ? data.detail : "Something went wrong.";
        setResult({ ok: false, text });
        setToast({ ok: false, text });
        return null;
      }
      const applied = data.ok !== false;
      const outcome = { ok: applied, text: applied ? successText || "Saved and applied." : (data.messages || []).join(" ") };
      setResult(outcome);
      setToast(outcome);
      await loadConfig();
      loadStatus();
      return data;
    } catch {
      setResult({ ok: false, text: "Connection error. Please try again." });
      setToast({ ok: false, text: "Connection error. Please try again." });
      return null;
    } finally {
      setBusy(false);
    }
  };

  if (loadError) return <section className="panel"><div className="admin-dash-error">{loadError}</div></section>;
  if (!config) return <section className="panel"><p className="panel-empty">Loading…</p></section>;

  const props = { config, status, send, busy, apiBase, reload: loadConfig };
  const switchTo = (id) => {
    chooseTenant(id);
    setResult(null);
    setConfig(null);
    setStatus(null);
    loadConfig();
    loadStatus();
  };

  return (
    <div className="pbx">
      <div className="idea-form-row pbx-tenant-bar">
        <label className="idea-field">
          Tenant
          <select className="idea-select" value={config.tenant.id} disabled={busy}
            onChange={(e) => switchTo(e.target.value)}>
            {config.tenants.map((t) => (
              <option key={t.id} value={t.id}>{t.code} · {t.name}{t.status === "active" ? "" : " (suspended)"}</option>
            ))}
          </select>
        </label>
        {!config.tenant.main && (
          <span className="pbx-tenant-flag">
            Working on {config.tenant.name} ({config.tenant.code}): every page here is theirs
          </span>
        )}
      </div>
      <div className="ticket-tabs pbx-sections">
        {SECTIONS.map((s) => (
          <button
            key={s.key}
            onClick={() => { setSection(s.key); setResult(null); }}
            className={`ticket-tab-btn ${section === s.key ? "ticket-tab-btn-active" : ""}`}
          >
            {s.label}
          </button>
        ))}
      </div>

      <Result result={result} />
      <SaveToast toast={toast} onClose={closeToast} />

      {section === "status" && <StatusSection {...props} />}
      {section === "call" && <ClickToCallSection {...props} initialNumber={callNumber} />}
      {section === "links" && <CallLinksSection {...props} />}
      {section === "extensions" && <ExtensionsSection {...props} />}
      {section === "trunks" && <TrunksSection {...props} />}
      {section === "inbound" && <InboundSection {...props} />}
      {section === "ivr" && <IvrSection {...props} />}
      {section === "hours" && <HoursSection {...props} />}
      {section === "recordings" && <RecordingsSection {...props} />}
      {section === "billing" && <BillingPanel apiBase={apiBase} />}
      {section === "tenants" && <TenantsSection {...props} switchTo={switchTo} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

const KIND_NAMES = { mobile: "mobiles", landline: "landlines", freephone: "freephone", international: "international" };

function StatusSection({ config, status, send, busy }) {
  if (!status) return <section className="panel"><p className="panel-empty">Checking Asterisk…</p></section>;
  if (!status.asterisk_running) {
    return (
      <section className="panel">
        <div className="admin-dash-error pbx-flush">
          Asterisk isn't responding. On the VPS, check it with <code>sudo systemctl status asterisk</code>
        </div>
      </section>
    );
  }

  const exts = config.extensions.filter((e) => e.enabled);
  const registered = exts.filter((e) => status.extensions[e.ext]?.status === "Avail");
  const { settings, trunks } = config;
  const enabledOrder = settings.trunk_order.filter((t) => settings.trunks_enabled[t]);
  // Per trunk: the kinds of number it's tried first for, and those it's the fallback for.
  const roles = (key) => {
    const r = { first: [], fallback: [] };
    for (const kind of Object.keys(config.number_types)) {
      const route = settings.routes[kind];
      const used = route === "default" ? enabledOrder : enabledOrder.filter((t) => t === route);
      const i = used.indexOf(key);
      if (i === 0) r.first.push(KIND_NAMES[kind]);
      else if (i > 0) r.fallback.push(KIND_NAMES[kind]);
    }
    return r;
  };

  return (
    <>
      {status.last_apply?.ok === false && (
        <div className="admin-dash-error">Last change wasn't applied: {status.last_apply.messages.join(" ")}</div>
      )}

      <section className="pbx-trunk-grid">
        {settings.trunk_order.map((key) => {
          const s = status.trunks[key] || {};
          const { first, fallback } = roles(key);
          return (
            <div key={key} className={`vital-card vital-${statusLevel(s.status)}`}>
              <div className="vital-card-top">
                <span className="vital-label">{trunks[key].label}</span>
                <StatusDot status={s.status} />
              </div>
              <div className="pbx-trunk-status">{s.status === "Avail" ? "Reachable" : s.status || "Unknown"}</div>
              <div className="vital-detail">
                {trunks[key].host}
                {s.rtt_ms != null && ` · ${s.rtt_ms} ms`}
              </div>
              <div className="vital-detail">
                Calls now: {status.channels.filter((c) => c.channel.startsWith(`PJSIP/${key}-`)).length}
                {settings.trunk_max?.[key] ? ` of ${settings.trunk_max[key]}` : ""}
              </div>
              {!settings.trunks_enabled[key] ? (
                <div className="vital-detail">Outbound: off</div>
              ) : !first.length && !fallback.length ? (
                <div className="vital-detail">Outbound: not used for any calls</div>
              ) : (
                <>
                  {first.length > 0 && <div className="vital-detail">Main route: {first.join(", ")}</div>}
                  {fallback.length > 0 && <div className="vital-detail">Fallback: {fallback.join(", ")}</div>}
                </>
              )}
            </div>
          );
        })}
        <div className="vital-card vital-ok">
          <div className="vital-card-top"><span className="vital-label">Phones registered</span></div>
          <div className="vital-number">
            {registered.length}<span className="vital-unit"> / {exts.length}</span>
          </div>
          <div className="vital-detail">{status.channels.length} active call leg{status.channels.length === 1 ? "" : "s"}</div>
          <div className="vital-detail">{(status.version || "").split("~")[0].replace(/ built by.*/, "")}</div>
        </div>
      </section>

      <section className="panel pbx-gap">
        <h2>Active calls</h2>
        {status.channels.length === 0 ? (
          <p className="panel-empty">No calls in progress.</p>
        ) : (
          <table className="users-table">
            <thead>
              <tr><th>Channel</th><th>Caller</th><th>Dialled</th><th>State</th><th>Duration</th><th>Recording</th></tr>
            </thead>
            <tbody>
              {status.channels.map((c) => (
                <tr key={c.channel}>
                  <td>{c.channel}</td>
                  <td>{c.caller_id || "—"}</td>
                  <td>{c.exten || "—"}</td>
                  <td>{c.state}</td>
                  <td>{formatDuration(c.duration_seconds)}</td>
                  <td>
                    {c.recording ? <span className="pbx-chip pbx-recording">● Recording</span>
                      : c.recordable ? (
                        <button className="ticket-action-btn" disabled={busy}
                          title="Records from now on. The other person isn't told automatically."
                          onClick={() => send("POST", "/recordings/start", { channel: c.channel }, "Recording this call from now on.")}
                        >Record</button>
                      ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel pbx-gap">
        <h2>Phones</h2>
        <table className="users-table">
          <thead>
            <tr><th>Ext</th><th>Name</th><th>Status</th><th>From</th><th>Latency</th></tr>
          </thead>
          <tbody>
            {exts.map((e) => {
              const s = status.extensions[e.ext];
              return (
                <tr key={e.ext}>
                  <td>{e.ext}</td>
                  <td>{e.name}</td>
                  <td>
                    <StatusDot status={s?.status || "Unavail"} />{" "}
                    {!s ? "Not registered" : s.status === "Avail" ? "Registered" : s.status === "Unavail" ? "Not responding" : s.status}
                  </td>
                  <td>{s?.address || "—"}</td>
                  <td>{s?.rtt_ms != null ? `${s.rtt_ms} ms` : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="panel pbx-gap">
        <div className="idea-form-row">
          <button className="ticket-action-btn" disabled={busy}
            onClick={() => send("POST", "/apply", null, "Rewritten from the dashboard's settings and reloaded.")}>
            Re-apply to Asterisk</button>
          <span className="pbx-hint">
            Rewrites Asterisk's config from the dashboard and reloads it, changing nothing: for example after editing
            pjsip.conf by hand.{status.last_apply?.at ? ` Last applied ${new Date(status.last_apply.at).toLocaleString()}.` : ""}
          </span>
        </div>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Extensions
// ---------------------------------------------------------------------------

function CopyField({ label, value }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="pbx-cred-row">
      <span className="pbx-cred-label">{label}</span>
      <code className="pbx-cred-value">{value || "—"}</code>
      {value && (
        <button
          className="ticket-action-btn"
          onClick={async () => {
            try { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* no clipboard */ }
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      )}
    </div>
  );
}

const SETUP_APPS = {
  groundwire: {
    label: "Groundwire",
    how: "Install Groundwire, then scan this with the iPhone's Camera app and tap the link.",
  },
  linphone: {
    label: "Linphone",
    how: "Install Linphone, tap \u201cScan QR code\u201d on its setup screen and scan this from there, not with the Camera app. Linphone won't ring while it's closed: it can't get push notifications from this server.",
  },
};

function SetupQr({ ext, savedEmail, apiBase, reload }) {
  const [link, setLink] = useState(null); // { app, url, expires_at, qr }
  const [error, setError] = useState("");
  const [mail, setMail] = useState({ to: savedEmail || "", apps: ["groundwire"] });
  const [mailResult, setMailResult] = useState(null); // { ok, text }
  const [sending, setSending] = useState(false);

  const email = async (ev) => {
    ev.preventDefault();
    setSending(true);
    setMailResult(null);
    const res = await fetch(`${apiBase}/api/pbx/extensions/${ext}/email-setup`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tenantHeaders() },
      body: JSON.stringify(mail),
    });
    const data = await res.json().catch(() => ({}));
    setSending(false);
    setMailResult(res.ok ? { ok: true, text: data.messages?.join(" ") } : { ok: false, text: data.detail || "Email not sent." });
    if (res.ok) reload(); // so the address is offered next time
  };

  const make = async (app) => {
    setError("");
    const res = await fetch(`${apiBase}/api/pbx/extensions/${ext}/setup-link`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tenantHeaders() },
      body: JSON.stringify({ app }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) setLink({ app, ...data });
    else setError(data.detail || "Couldn't make a setup link.");
  };

  return (
    <div className="pbx-qr">
      {Object.entries(SETUP_APPS).map(([app, { label }]) => (
        <button
          key={app}
          className={`ticket-action-btn${app === "groundwire" ? " ticket-action-btn-primary" : ""}`}
          onClick={() => make(app)}
        >
          {link?.app === app ? `New QR code for ${label}` : `QR code for ${label}`}
        </button>
      ))}
      {error && <p className="pbx-hint">{error}</p>}
      {link && (
        <>
          <img className="pbx-qr-img" src={link.qr} alt={`${SETUP_APPS[link.app].label} setup QR code for extension ${ext}`} />
          <p className="pbx-hint">
            <b>{SETUP_APPS[link.app].label}:</b> {SETUP_APPS[link.app].how}{" "}
            It works once, until {new Date(link.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.
            A new password cancels it.
          </p>
        </>
      )}
      <form className="idea-form-row pbx-gap" onSubmit={email}>
        <input
          className="idea-input"
          type="email"
          placeholder="Client's email address"
          value={mail.to}
          onChange={(e) => setMail({ ...mail, to: e.target.value })}
        />
        {Object.entries(SETUP_APPS).map(([app, { label }]) => (
          <label key={app} className="pbx-toggle">
            <input
              type="checkbox"
              checked={mail.apps.includes(app)}
              onChange={(e) => setMail({
                ...mail,
                apps: e.target.checked ? [...mail.apps, app] : mail.apps.filter((a) => a !== app),
              })}
            />
            {label}
          </label>
        ))}
        <button className="ticket-action-btn" disabled={sending || !mail.to.trim() || !mail.apps.length}>
          {sending ? "Sending…" : "Email setup"}
        </button>
      </form>
      <p className="pbx-hint">
        The email has a link to tap on the iPhone and QR codes for reading it on a computer, plus the voicemail PIN.
        The link works once, for 72 hours.
      </p>
      {mailResult && (
        <div className={`ticket-action-result ${mailResult.ok ? "" : "ticket-action-result-error"}`}>{mailResult.text}</div>
      )}
    </div>
  );
}

function VoicemailEmail({ ext, current, send, busy }) {
  const [value, setValue] = useState(current || "");
  useEffect(() => setValue(current || ""), [current]);
  const save = (ev) => {
    ev.preventDefault();
    send("PUT", `/extensions/${ext}/voicemail-email`, { email: value.trim() },
      value.trim() ? `New voicemails for ${ext} will be emailed to ${value.trim()}.` : `Voicemails for ${ext} won't be emailed.`);
  };
  return (
    <form className="idea-form-row pbx-gap" onSubmit={save}>
      <input
        className="idea-input"
        type="email"
        placeholder="Email new voicemails to (optional)"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <button className="ticket-action-btn" disabled={busy || value.trim() === (current || "")}>Save</button>
      <span className="pbx-hint">With the recording attached. Leave blank to turn off.</span>
    </form>
  );
}

function OutboundCid({ ext, current, fallback, dids, send, busy }) {
  const [value, setValue] = useState(current || "");
  useEffect(() => setValue(current || ""), [current]);
  const clean = value.replace(/\s/g, "");
  const save = (ev) => {
    ev.preventDefault();
    send("PUT", `/extensions/${ext}/outbound-cid`, { outbound_cid: clean },
      clean ? `Calls from ${ext} will show ${clean}.` : `Calls from ${ext} will show the default caller ID.`);
  };
  return (
    <form className="idea-form-row pbx-gap" onSubmit={save}>
      <input
        className="idea-input"
        placeholder={fallback ? `Caller ID (default ${fallback})` : "Caller ID (optional)"}
        list="pbx-dids"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <datalist id="pbx-dids">
        {dids.map((d) => <option key={d} value={d} />)}
      </datalist>
      <button className="ticket-action-btn" disabled={busy || clean === (current || "")}>Save</button>
      <span className="pbx-hint">Shown to people this extension calls, e.g. a company number or the user's mobile so calls come back to them. Leave blank for the default.</span>
    </form>
  );
}

const FORWARD_FIELDS = [
  ["always", "Forward all calls to"],
  ["busy", "When busy, forward to"],
  ["no_answer", "When not answered or offline, forward to"],
];
const NO_FORWARDING = { dnd: false, always: "", busy: "", no_answer: "" };

function Forwarding({ ext, current, available, send, busy, reload }) {
  const saved = current || NO_FORWARDING;
  const [form, setForm] = useState(saved);
  useEffect(() => setForm(current || NO_FORWARDING), [current]);
  const changed = FORWARD_FIELDS.some(([k]) => form[k].trim() !== saved[k]) || form.dnd !== saved.dnd;
  const save = async (ev) => {
    ev.preventDefault();
    if (await send("PUT", `/extensions/${ext}/forwarding`, form, `Call forwarding for ${ext} saved.`)) reload();
  };
  if (!available) return <p className="pbx-hint">Call forwarding: Asterisk isn't answering, so it can't be shown.</p>;
  return (
    <form className="pbx-forwarding" onSubmit={save}>
      <div className="ticket-action-label">Call forwarding and do not disturb (also set from the phone, see feature codes)</div>
      <label className="pbx-toggle">
        <input type="checkbox" checked={form.dnd} onChange={(e) => setForm({ ...form, dnd: e.target.checked })} />
        Do not disturb: callers get voicemail (or the busy forward), and ring groups skip {ext}
      </label>
      {FORWARD_FIELDS.map(([k, label]) => (
        <label key={k} className="pbx-field">
          <span>{label}</span>
          <input
            className="idea-input"
            placeholder="Extension or Irish number, e.g. 202 or 087 123 4567"
            value={form[k]}
            onChange={(e) => setForm({ ...form, [k]: e.target.value })}
          />
        </label>
      ))}
      <button className="ticket-action-btn" disabled={busy || !changed}>Save forwarding</button>
      <span className="pbx-hint"> Only extensions and Irish landlines and mobiles. Calls to outside numbers use the price cap and are billed to {ext}.</span>
    </form>
  );
}

function forwardingBadge(f) {
  if (!f) return null;
  if (f.dnd) return "Do not disturb";
  if (f.always) return `Forwarded to ${f.always}`;
  const parts = [f.busy && `busy → ${f.busy}`, f.no_answer && `no answer → ${f.no_answer}`].filter(Boolean);
  return parts.length ? `Forwards ${parts.join(", ")}` : null;
}

function ExtensionsSection({ config, status, send, busy, apiBase, reload }) {
  const [draft, setDraft] = useState({ ext: "", name: "" });
  const [editing, setEditing] = useState(null); // { ext, name }
  const [creds, setCreds] = useState(null); // { ext, ...credentials }
  const [forwarding, setForwarding] = useState({ available: true, extensions: {} });
  const { limits } = config;

  const loadForwarding = useCallback(async () => {
    try {
      const res = await fetch(`${apiBase}/api/pbx/forwarding`);
      if (res.ok) setForwarding(await res.json());
    } catch { /* shown as unavailable on the next try */ }
  }, [apiBase]);
  useEffect(() => { loadForwarding(); }, [loadForwarding, config]);

  const add = async (e) => {
    e.preventDefault();
    const data = await send("POST", "/extensions", { ext: draft.ext.trim() || null, name: draft.name },
      "Extension created.");
    if (data) {
      setDraft({ ext: "", name: "" });
      showCreds(data.ext);
    }
  };

  const showCreds = async (ext) => {
    if (creds?.ext === ext) return setCreds(null);
    const res = await fetch(`${apiBase}/api/pbx/extensions/${ext}/credentials`);
    if (res.ok) setCreds({ ext, ...(await res.json()) });
  };

  return (
    <section className="panel">
      <h2>Extensions</h2>
      <label className="pbx-toggle pbx-gap">
        <input
          type="checkbox"
          checked={config.settings.video}
          disabled={busy}
          onChange={(ev) => send("PUT", "/settings", { ...config.settings, video: ev.target.checked },
            ev.target.checked ? "Video calls between extensions turned on." : "Video calls turned off: calls are audio only.")}
        />
        Video calls between extensions
      </label>
      <span className="pbx-hint"> Groundwire and Linphone (VP8/H.264). Calls to and from outside numbers stay audio only.</span>
      <details className="pbx-gap">
        <summary className="pbx-hint">Feature codes for the phones</summary>
        <table className="users-table">
          <tbody>
            <tr><td><code>*78</code> / <code>*79</code></td><td>Do not disturb on / off</td></tr>
            <tr><td><code>*72</code>number / <code>*73</code></td><td>Forward all calls / stop</td></tr>
            <tr><td><code>*90</code>number / <code>*91</code></td><td>Forward when busy / stop</td></tr>
            <tr><td><code>*52</code>number / <code>*53</code></td><td>Forward when not answered or offline / stop</td></tr>
            <tr><td><code>*8</code></td><td>Answer any ringing extension</td></tr>
            <tr><td><code>**</code>ext, e.g. <code>**202</code></td><td>Answer that extension's ringing call</td></tr>
            <tr><td><code>*97</code> / <code>*98</code></td><td>Your voicemail / any mailbox</td></tr>
            <tr><td><code>700</code></td><td>Park a call: transfer it to 700 and note the slot read out (701–720)</td></tr>
            <tr><td><code>701</code>–<code>720</code></td><td>Pick up the call parked in that slot</td></tr>
          </tbody>
        </table>
        <p className="pbx-hint">Forward to an extension or an Irish number in national format, e.g. *720871234567.
          Transfers use the app's or phone's own transfer button. A parked call nobody picks up rings the phone that
          parked it again after 45 seconds. Add 701, 702… as Quick Dials (BLF) in Groundwire to see which slots are in use.</p>
      </details>
      <form className="idea-form-row pbx-add-row" onSubmit={add}>
        <input
          className="idea-input pbx-num-input"
          placeholder="Number"
          inputMode="numeric"
          title={`Leave blank for the next free number (${limits.ext_min}-${limits.ext_max})`}
          value={draft.ext}
          onChange={(e) => setDraft({ ...draft, ext: e.target.value })}
        />
        <input
          className="idea-input pbx-name-input"
          placeholder="Name, e.g. Reception"
          maxLength={40}
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        />
        <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !draft.name.trim()}>
          Add extension
        </button>
        <span className="pbx-hint">Leave the number blank to use the next free one.</span>
      </form>

      <table className="users-table">
        <thead>
          <tr><th>Ext</th><th>Name</th><th>Phone</th><th>Enabled</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {config.extensions.flatMap((e) => {
            const s = status?.extensions?.[e.ext];
            const rows = [
              <tr key={e.ext} className={e.enabled ? "" : "pbx-row-disabled"}>
                <td>{e.ext}</td>
                <td>
                  {editing?.ext === e.ext ? (
                    <span className="pbx-inline-edit">
                      <input
                        className="idea-input"
                        maxLength={40}
                        value={editing.name}
                        autoFocus
                        onChange={(ev) => setEditing({ ...editing, name: ev.target.value })}
                      />
                      <button
                        className="ticket-action-btn ticket-action-btn-primary"
                        disabled={busy || !editing.name.trim()}
                        onClick={async () => {
                          if (await send("PUT", `/extensions/${e.ext}`, { name: editing.name, enabled: e.enabled }, "Name updated.")) setEditing(null);
                        }}
                      >Save</button>
                      <button className="ticket-action-btn" onClick={() => setEditing(null)}>Cancel</button>
                    </span>
                  ) : e.name}
                  {e.outbound_cid && <div className="pbx-hint">Shows {e.outbound_cid}</div>}
                  {forwardingBadge(forwarding.extensions[e.ext]) && (
                    <div className="pbx-hint">{forwardingBadge(forwarding.extensions[e.ext])}</div>
                  )}
                </td>
                <td>
                  {!e.enabled ? "—" : (
                    <><StatusDot status={s?.status || "Unavail"} /> {s?.status === "Avail" ? "Registered" : "Offline"}</>
                  )}
                </td>
                <td>
                  <label className="pbx-toggle">
                    <input
                      type="checkbox"
                      checked={e.enabled}
                      disabled={busy}
                      onChange={() => send("PUT", `/extensions/${e.ext}`, { name: e.name, enabled: !e.enabled },
                        e.enabled ? `Extension ${e.ext} disabled. Its phone will be logged out.` : `Extension ${e.ext} enabled.`)}
                    />
                    {e.enabled ? "On" : "Off"}
                  </label>
                </td>
                <td className="pbx-actions">
                  <button className="ticket-action-btn" onClick={() => setEditing({ ext: e.ext, name: e.name })}>Rename</button>
                  <button className="ticket-action-btn" onClick={() => showCreds(e.ext)}>
                    {creds?.ext === e.ext ? "Hide setup" : "Phone setup"}
                  </button>
                  <button
                    className="ticket-action-btn"
                    disabled={busy}
                    onClick={async () => {
                      if (!window.confirm(`Give extension ${e.ext} a new password? The phone using it will stop working until you enter the new one.`)) return;
                      if (await send("POST", `/extensions/${e.ext}/reset-password`, null, "New password set.")) {
                        setCreds(null);
                        showCreds(e.ext);
                      }
                    }}
                  >New password</button>
                  <button
                    className="ticket-action-btn idea-delete-btn"
                    disabled={busy}
                    onClick={() => window.confirm(`Delete extension ${e.ext} (${e.name})? This can't be undone.`) &&
                      send("DELETE", `/extensions/${e.ext}`, null, `Extension ${e.ext} deleted.`)}
                  >Delete</button>
                </td>
              </tr>,
            ];
            if (creds?.ext === e.ext) {
              rows.push(
                <tr key={`${e.ext}-creds`}>
                  <td colSpan={5} className="ticket-action-panel">
                    <SetupQr key={e.ext} ext={e.ext} savedEmail={e.setup_email} apiBase={apiBase} reload={reload} />
                    <div className="ticket-action-label">Or enter these in the phone app for extension {e.ext}:</div>
                    <CopyField label="Server" value={creds.server} />
                    <CopyField label="Port" value={creds.port} />
                    <CopyField label="Transport" value={creds.transport} />
                    <CopyField label="Encryption" value={creds.media_encryption} />
                    <CopyField label="Username" value={creds.username} />
                    <CopyField label="Password" value={creds.password} />
                    <CopyField label="Voicemail" value={creds.voicemail_number} />
                    <CopyField label="Voicemail PIN" value={creds.voicemail_pin} />
                    <button
                      className="ticket-action-btn"
                      disabled={busy}
                      onClick={async () => {
                        if (await send("POST", `/extensions/${e.ext}/reset-voicemail-pin`, null, "New voicemail PIN set.")) {
                          setCreds(null);
                          showCreds(e.ext);
                        }
                      }}
                    >New voicemail PIN</button>
                    <VoicemailEmail ext={e.ext} current={e.voicemail_email} send={send} busy={busy} />
                    <OutboundCid ext={e.ext} current={e.outbound_cid} fallback={config.settings.outbound_cid}
                      dids={config.inbound_routes.map((r) => r.did)} send={send} busy={busy} />
                    <label className="pbx-toggle pbx-gap">
                      <input type="checkbox" checked={e.record} disabled={busy}
                        onChange={(ev) => send("PUT", `/extensions/${e.ext}/recording`, { always: ev.target.checked },
                          ev.target.checked ? `Every call ${e.ext} answers or makes is recorded from now on.` : `${e.ext}'s calls are no longer recorded.`)} />
                      Record every call (see Recordings for the notice callers hear)
                    </label>
                    <Forwarding ext={e.ext} current={forwarding.extensions[e.ext]} available={forwarding.available}
                      send={send} busy={busy} reload={loadForwarding} />
                    {!creds.server && <p className="pbx-hint">Set PBX_PUBLIC_HOST in the dashboard's .env to show the server name here.</p>}
                  </td>
                </tr>
              );
            }
            return rows;
          })}
        </tbody>
      </table>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Click to call
// ---------------------------------------------------------------------------

const C2C_EXT_KEY = "pbx-click-to-call-ext";

// tel:+353-87-123-4567;ext=12 -> +353871234567 (parameters dropped)
export function numberFromTel(value) {
  let v = value || "";
  try { v = decodeURIComponent(v); } catch { /* use as is */ }
  v = v.replace(/^tel:/i, "").split(";")[0];
  return v.replace(/[^\d+*#]/g, "");
}

function ClickToCallSection({ config, send, busy, apiBase, initialNumber }) {
  const enabled = config.extensions.filter((e) => e.enabled);
  const [ext, setExt] = useState(() => {
    try { return localStorage.getItem(C2C_EXT_KEY) || ""; } catch { return ""; }
  });
  const [number, setNumber] = useState(initialNumber || "");
  const [route, setRoute] = useState(null);
  const [handler, setHandler] = useState("");
  useEffect(() => { if (initialNumber) setNumber(initialNumber); }, [initialNumber]);
  const from = enabled.some((e) => e.ext === ext) ? ext : enabled[0]?.ext || "";
  const isExt = /^2\d\d$/.test(number.trim());

  // What the call would do, before it's made: the route and price, or why
  // it would be refused. Numbers from tel: links come from any web page.
  useEffect(() => {
    const n = number.trim();
    if (!n || /^2\d\d$/.test(n)) { setRoute(null); return undefined; }
    const t = setTimeout(async () => {
      try {
        const res = await fetch(`${apiBase}/api/pbx/route-test?${new URLSearchParams({ number: n })}`);
        if (res.ok) setRoute(await res.json());
      } catch { setRoute(null); }
    }, 400);
    return () => clearTimeout(t);
  }, [number, apiBase]);

  const call = async (ev) => {
    ev.preventDefault();
    try { localStorage.setItem(C2C_EXT_KEY, from); } catch { /* not remembered */ }
    await send("POST", "/click-to-call", { ext: from, number },
      `Ringing extension ${from}. Answer it and the call to ${number.trim()} starts.`);
  };

  const registerTel = () => {
    try {
      navigator.registerProtocolHandler("tel", `${window.location.origin}${window.location.pathname}?call=%s`);
      setHandler("Asked the browser. Accept its prompt, then tel: links open here with the number filled in.");
    } catch (e) {
      setHandler(`This browser wouldn't do it: ${e.message}`);
    }
  };

  const r = route?.result;
  const blocked = !isExt && route && (!route.recognised || !!r?.refuse || !!r?.note);
  let preview = null;
  if (isExt) preview = <span>Extension {number.trim()}</span>;
  else if (route && !route.recognised) preview = <span className="pbx-warning">Not a number the phones can call.</span>;
  else if (r?.note) preview = <span className="pbx-warning">Dial emergency numbers from the phone itself.</span>;
  else if (r?.refuse) preview = <span className="pbx-warning">Would be refused: {r.refuse}</span>;
  else if (r?.routes?.length) {
    const first = r.candidates.find((c) => !c.dropped);
    preview = <span>{r.kind}, via {r.routes[0].label}{first?.per_min ? `, €${first.per_min}/min` : ""}</span>;
  }

  return (
    <section className="panel">
      <h2>Click to call</h2>
      <p className="pbx-hint">
        Your extension rings first, showing the number. Answer it and the call goes out as if you&rsquo;d dialled it:
        your caller ID, the price cap, billed to your extension. It rings even on do not disturb.
      </p>
      <form className="idea-form-row pbx-add-row" onSubmit={call}>
        <select className="idea-select pbx-select" value={from} onChange={(e) => setExt(e.target.value)}>
          {enabled.map((e) => <option key={e.ext} value={e.ext}>{e.ext} {e.name}</option>)}
        </select>
        <input
          className="idea-input"
          placeholder="Number, e.g. 087 123 4567, +44 20 8759 9036 or 202"
          value={number}
          onChange={(e) => setNumber(e.target.value)}
        />
        <button className="ticket-action-btn ticket-action-btn-primary"
          disabled={busy || !from || !number.trim() || blocked}>Call</button>
      </form>
      {preview && <div className="pbx-hint">{preview}</div>}
      {initialNumber && <p className="pbx-hint">Filled in from a tel: link. Check the number before you call.</p>}

      <div className="ticket-action-label pbx-gap">Call from links on other web pages</div>
      <p className="pbx-hint">
        Make this dashboard the browser&rsquo;s handler for phone number (tel:) links, e.g. in a CRM. Clicking one opens
        this page with the number filled in; it never calls until you press Call. Chrome, Edge and Firefox; not Safari.
      </p>
      {typeof navigator.registerProtocolHandler === "function" ? (
        <button className="ticket-action-btn" onClick={registerTel}>Open tel: links here</button>
      ) : <p className="pbx-hint">This browser can&rsquo;t do this.</p>}
      {handler && <p className="pbx-hint">{handler}</p>}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Call links: a URL anyone can open to call from their browser
// ---------------------------------------------------------------------------

const EMPTY_LINK = { label: "", target: "", video: false, max_minutes: 30, enabled: true };

function linkSnippets(link) {
  const esc = (v) => v.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  return {
    signature: `<a href="${esc(link.url)}">Call me from your browser</a>`,
    button: `<a href="${esc(link.url)}" target="_blank" rel="noopener" style="display:inline-block;padding:12px 20px;border-radius:8px;background:#1f8a4c;color:#fff;text-decoration:none;font:600 16px sans-serif">Call us</a>`,
    iframe: `<iframe src="${esc(link.url)}" allow="microphone; camera" width="420" height="520" style="border:0"></iframe>`,
  };
}

function CallLinksSection({ config, send, busy, apiBase }) {
  const [data, setData] = useState(null);
  const [form, setForm] = useState(null); // null | { ...fields, id? }
  const [open, setOpen] = useState(null); // id whose snippets are showing

  const load = useCallback(async () => {
    const res = await fetch(`${apiBase}/api/pbx/call-links`);
    if (res.ok) setData(await res.json());
  }, [apiBase]);
  useEffect(() => { load(); }, [load, config]);

  const targets = [
    ...config.extensions.filter((e) => e.enabled).map((e) => ({ value: e.ext, label: `Extension ${e.ext} ${e.name}` })),
    ...config.ring_groups.map((g) => ({ value: g.number, label: `Ring group ${g.number} ${g.name}` })),
    ...config.ivr_menus.map((m) => ({ value: m.number, label: `IVR menu ${m.number} ${m.name}` })),
    ...config.business_hours.map((h) => ({ value: h.number, label: `Business hours ${h.number} ${h.name}` })),
  ];
  const targetName = (t) => targets.find((x) => x.value === t)?.label || `${t} (switched off or gone)`;

  const save = async (ev) => {
    ev.preventDefault();
    const body = { ...form, target: form.target || targets[0]?.value || "", max_minutes: Number(form.max_minutes) };
    const ok = form.id
      ? await send("PUT", `/call-links/${form.id}`, body, "Call link saved.")
      : await send("POST", "/call-links", body, "Call link created. Copy its URL or a snippet below.");
    if (ok) { setForm(null); load(); }
  };

  if (!data) return <section className="panel"><p className="panel-empty">Loading…</p></section>;

  return (
    <section className="panel">
      <h2>Call links</h2>
      <p className="pbx-hint">
        A link anyone can open to call an extension, ring group or IVR menu (with a keypad) from their browser, with no app or account: for an
        email signature, a website or a QR code. Browsers can only reach the targets of links that are switched on,
        never an outside line. At most 2 calls at once per link and 5 in all.
      </p>
      {!data.ready && (
        <div className="pbx-warning">Set PBX_PROVISION_BASE_URL and PBX_PUBLIC_HOST in the dashboard&rsquo;s .env, and set up
          /call/ and /pbx-ws in nginx (see the README), before links can work.</div>
      )}
      {!form && <button className="ticket-action-btn ticket-action-btn-primary pbx-gap" onClick={() => setForm({ ...EMPTY_LINK })}>New call link</button>}

      {form && (
        <form className="pbx-forwarding" onSubmit={save}>
          <label className="pbx-field"><span>Name (shown on the page and on the phone)</span>
            <input className="idea-input" maxLength={40} placeholder="e.g. Sales website" value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })} />
          </label>
          <label className="pbx-field"><span>Rings</span>
            <select className="idea-select pbx-select" value={form.target || targets[0]?.value || ""}
              onChange={(e) => setForm({ ...form, target: e.target.value })}>
              {targets.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </label>
          <label className="pbx-field"><span>Longest call (minutes)</span>
            <input className="idea-input pbx-num-input" type="number" min={1} max={data.max_minutes} value={form.max_minutes}
              onChange={(e) => setForm({ ...form, max_minutes: e.target.value })} />
          </label>
          <label className="pbx-toggle">
            <input type="checkbox" checked={form.video} onChange={(e) => setForm({ ...form, video: e.target.checked })} />
            Allow video{!data.video_on && " (also turn on video calls under Extensions)"}
          </label>
          <label className="pbx-toggle">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
            Switched on
          </label>
          <div className="pbx-actions">
            <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !form.label.trim()}>
              {form.id ? "Save" : "Create link"}</button>
            <button type="button" className="ticket-action-btn" onClick={() => setForm(null)}>Cancel</button>
          </div>
        </form>
      )}

      {data.links.length === 0 ? <p className="panel-empty">No call links yet.</p> : (
        <table className="users-table pbx-gap">
          <thead><tr><th>Name</th><th>Rings</th><th>Options</th><th>Last opened</th><th>Actions</th></tr></thead>
          <tbody>
            {data.links.flatMap((l) => {
              const rows = [
                <tr key={l.id} className={l.live ? "" : "pbx-row-disabled"}>
                  <td>{l.label}{!l.live && <div className="pbx-hint">{l.enabled ? "Not working: its target is off or gone" : "Switched off"}</div>}</td>
                  <td>{targetName(l.target)}</td>
                  <td>{l.video ? "Video, " : ""}{l.max_minutes} min</td>
                  <td>{l.last_opened_at ? new Date(l.last_opened_at).toLocaleString() : <span className="pbx-hint">Never</span>}</td>
                  <td className="pbx-actions">
                    <button className="ticket-action-btn" onClick={() => setOpen(open === l.id ? null : l.id)}>
                      {open === l.id ? "Hide" : "Link & snippets"}</button>
                    <button className="ticket-action-btn" onClick={() => setForm({ ...l })}>Edit</button>
                    <button className="ticket-action-btn" disabled={busy}
                      onClick={async () => window.confirm(`Give "${l.label}" a new URL? The current one stops working straight away, wherever it's been shared.`) &&
                        (await send("POST", `/call-links/${l.id}/new-url`, null, "New URL made. Update it wherever the link is shared.")) && load()}
                    >New URL</button>
                    <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                      onClick={async () => window.confirm(`Delete "${l.label}"? The link stops working wherever it's been shared.`) &&
                        (await send("DELETE", `/call-links/${l.id}`, null, "Call link deleted.")) && load()}
                    >Delete</button>
                  </td>
                </tr>,
              ];
              if (open === l.id) {
                const sn = linkSnippets(l);
                rows.push(
                  <tr key={`${l.id}-snippets`}>
                    <td colSpan={5} className="ticket-action-panel">
                      <CopyField label="Link" value={l.url} />
                      {l.qr && <img className="pbx-qr-img" src={l.qr} alt={`QR code for ${l.label}`} />}
                      <div className="ticket-action-label">Email signature</div>
                      <CopyField label="HTML" value={sn.signature} />
                      <div className="ticket-action-label">Website button (opens the call in a new tab)</div>
                      <CopyField label="HTML" value={sn.button} />
                      <div className="ticket-action-label">Website, built into the page</div>
                      <CopyField label="HTML" value={sn.iframe} />
                      <p className="pbx-hint">Built in only works if the website allows it; the button always works.</p>
                    </td>
                  </tr>
                );
              }
              return rows;
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Trunks & outbound
// ---------------------------------------------------------------------------

function TrunksSection({ config, status, send, busy, apiBase }) {
  const [form, setForm] = useState(config.settings);
  useEffect(() => setForm(config.settings), [config.settings]);
  const { trunks } = config;

  const move = (i, dir) => {
    const order = [...form.trunk_order];
    [order[i], order[i + dir]] = [order[i + dir], order[i]];
    setForm({ ...form, trunk_order: order });
  };
  const dirty = JSON.stringify(form) !== JSON.stringify(config.settings);
  const noneEnabled = !form.trunk_order.some((t) => form.trunks_enabled[t]);

  return (
    <>
    <section className="panel">
      <h2>Outbound trunks</h2>
      <p className="pbx-hint pbx-intro">
        Calls go out through the first enabled trunk, unless the table below sends that kind of number elsewhere.
        The next one is only tried if that trunk can't take the call (down, refused or congested), or it&rsquo;s at
        its most calls at once: set that to match what the carrier allows, counting calls in and out. When every
        trunk is full or down the caller hears the engaged tone. 112/999 always go via Irish VoIP, whatever is set
        here, and are never refused for being over the most calls.
      </p>
      <ol className="pbx-trunk-order">
        {form.trunk_order.map((key, i) => {
          const s = status?.trunks?.[key];
          return (
            <li key={key} className="idea-item pbx-trunk-row">
              <span className="pbx-order-num">{i + 1}</span>
              <StatusDot status={s?.status} />
              <span className="idea-title">{trunks[key].label}</span>
              <span className="pbx-hint">{trunks[key].host}{s?.rtt_ms != null && ` · ${s.rtt_ms} ms`}</span>
              <label className="pbx-toggle pbx-push">
                <input
                  type="checkbox"
                  checked={!!form.trunks_enabled[key]}
                  onChange={(e) => setForm({ ...form, trunks_enabled: { ...form.trunks_enabled, [key]: e.target.checked } })}
                />
                Use for outbound
              </label>
              <label className="idea-field" title="The most calls at once this carrier allows (its channels, in and out together), as set with the carrier. Empty: no ceiling here.">
                Most calls
                <input
                  className="idea-input pbx-num-input"
                  type="number"
                  min={1}
                  max={200}
                  placeholder="—"
                  value={form.trunk_max?.[key] ?? ""}
                  onChange={(e) => setForm({ ...form, trunk_max: { ...form.trunk_max, [key]: e.target.value } })}
                />
              </label>
              <button type="button" className="ticket-action-btn" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up">↑</button>
              <button type="button" className="ticket-action-btn" disabled={i === form.trunk_order.length - 1} onClick={() => move(i, 1)} aria-label="Move down">↓</button>
            </li>
          );
        })}
      </ol>
      {noneEnabled && <div className="ticket-action-result ticket-action-result-error">With no trunk enabled, only extension-to-extension and emergency calls will work.</div>}

      <h3 className="pbx-subhead">Which trunk each kind of number uses</h3>
      <table className="users-table pbx-routes">
        <tbody>
          {Object.entries(config.number_types).map(([kind, label]) => (
            <tr key={kind}>
              <td>{label}</td>
              <td>
                <select
                  className="idea-select pbx-select"
                  value={form.routes[kind]}
                  onChange={(e) => setForm({ ...form, routes: { ...form.routes, [kind]: e.target.value } })}
                >
                  <option value="default">Trunk order above</option>
                  {form.trunk_order.map((t) => <option key={t} value={t}>{trunks[t].label} only</option>)}
                  <option value="cheapest">Cheapest route</option>
                  <option value="off">Blocked</option>
                </select>
                {trunks[form.routes[kind]] && !form.trunks_enabled[form.routes[kind]] && (
                  <small className="pbx-hint"> {trunks[form.routes[kind]].label} is off above, so these calls will fail.</small>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="pbx-hint">
        &ldquo;Only&rdquo; means no fallback: if that trunk can&rsquo;t take the call, it fails. &ldquo;Cheapest
        route&rdquo; tries the routes ticked below, cheapest first. Numbers that fit none of these (premium 15xx, short
        codes) are always refused.
      </p>

      <h3 className="pbx-subhead">Price cap and cheapest route</h3>
      <p className="pbx-hint pbx-intro">
        Before every outbound call (except 112/999) the dashboard drops any route whose rate sheet has no price for
        the number, or whose price is over the cap, in € per minute ex VAT. If none is left, the call is refused. If
        the dashboard doesn&rsquo;t answer (restarting or down), Irish numbers go out on the trunk order unchecked and
        international calls are refused, so keep a high safety cap on Voxbeam&rsquo;s portal too.
      </p>
      <div className="pbx-mapping">
        {[["default", "Default cap, € per minute *"], ...Object.keys(config.number_types).map((k) => [k, `${config.number_types[k].split(" (")[0]} cap`])].map(([k, label]) => (
          <label key={k} className="pbx-field">
            <span>{label}</span>
            <input className="idea-input" placeholder={k === "default" ? "0.05" : `default (${form.caps.default || "—"})`}
              value={form.caps[k]} onChange={(e) => setForm({ ...form, caps: { ...form.caps, [k]: e.target.value } })} />
          </label>
        ))}
      </div>
      <div className="ticket-action-label">Routes &ldquo;Cheapest route&rdquo; may use</div>
      <div className="pbx-member-grid">
        {config.all_routes.map((r) => (
          <label key={r.key} className="pbx-toggle">
            <input type="checkbox" checked={!!form.lcr_allow[r.key]}
              onChange={(e) => setForm({ ...form, lcr_allow: { ...form.lcr_allow, [r.key]: e.target.checked } })} />
            {r.label}
          </label>
        ))}
      </div>

      <div className="pbx-fields">
        <label className="pbx-field">
          <span>Caller ID shown to people you call</span>
          <input
            className="idea-input"
            placeholder="+35312345678"
            value={form.outbound_cid}
            onChange={(e) => setForm({ ...form, outbound_cid: e.target.value })}
          />
          <small className="pbx-hint">Shown on outbound calls from extensions without their own. An extension can have its own under Phone setup.</small>
        </label>
        <label className="pbx-field">
          <span>Voxbeam route</span>
          <select
            className="idea-select pbx-select"
            value={form.voxbeam_prefix}
            onChange={(e) => setForm({ ...form, voxbeam_prefix: e.target.value })}
          >
            {Object.entries(config.voxbeam_routes).map(([prefix, label]) => (
              <option key={prefix} value={prefix}>{label} ({prefix})</option>
            ))}
          </select>
        </label>
        <label className="pbx-field">
          <span>Max simultaneous outbound calls</span>
          <input
            className="idea-input pbx-num-input"
            type="number"
            min={1}
            max={30}
            value={form.max_outbound}
            onChange={(e) => setForm({ ...form, max_outbound: Number(e.target.value) })}
          />
          <small className="pbx-hint">
            {config.tenant.main ? "A brake on toll fraud: extra calls get a busy tone."
              : "Calls in and out together. Extra calls out get a busy tone; extra calls in go to the main mailbox."}
          </small>
        </label>
        {!config.tenant.main && (
          <label className="pbx-field">
            <span>Main mailbox</span>
            <select className="idea-select pbx-select" value={form.main_mailbox}
              onChange={(e) => setForm({ ...form, main_mailbox: e.target.value })}>
              <option value="">The lowest extension</option>
              {config.extensions.map((e) => <option key={e.ext} value={e.ext}>{e.ext} · {e.name}</option>)}
            </select>
            <small className="pbx-hint">Takes incoming calls over the limit.</small>
          </label>
        )}
      </div>
      {!config.tenant.main && (
        <p className="pbx-hint">
          Caller ID, calls at once, the main mailbox and price caps are {config.tenant.name}&rsquo;s own. Trunks, their
          order and the routes for each kind of number are the server&rsquo;s: changing them here changes them for every
          tenant.
        </p>
      )}

      <div className="ticket-action-buttons">
        <button className="ticket-action-btn" disabled={!dirty || busy} onClick={() => setForm(config.settings)}>Undo changes</button>
        <button
          className="ticket-action-btn ticket-action-btn-primary"
          disabled={!dirty || busy}
          onClick={() => send("PUT", "/settings", form, "Trunk settings saved and applied.")}
        >Save</button>
      </div>
    </section>
    <RouteTest apiBase={apiBase} />
    <Coverage apiBase={apiBase} numberTypes={config.number_types} />
    </>
  );
}

// Try a number against the saved routing, and see the recent real checks.
function RouteTest({ apiBase }) {
  const [number, setNumber] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  const run = async (e) => {
    e?.preventDefault();
    setError("");
    const res = await fetch(`${apiBase}/api/pbx/route-test?${new URLSearchParams({ number: number || "0" })}`);
    const d = await res.json().catch(() => ({}));
    if (res.ok) setData(d); else setError(d.detail || `Error ${res.status}`);
  };
  useEffect(() => { run(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const r = data?.result;
  const stale = data && data.last_outbound_call && data.last_outbound_call > data.started_at &&
    (!data.last_check || data.last_check < data.last_outbound_call);
  return (
    <section className="panel pbx-gap">
      <h2>Test a number</h2>
      <form className="idea-form-row" onSubmit={run}>
        <input className="idea-input" placeholder="e.g. +44 20 8759 9036 or 087 123 4567" value={number}
          onChange={(e) => setNumber(e.target.value)} />
        <button className="ticket-action-btn ticket-action-btn-primary" disabled={!number.trim()}>Check</button>
        <span className="pbx-hint">Uses the saved settings: save changes above first.</span>
      </form>
      {error && <div className="ticket-action-result ticket-action-result-error">{error}</div>}
      {data && number && !data.recognised && <p className="pbx-hint">That number would be refused: it isn&rsquo;t a number type the phones can call.</p>}
      {r && r.note && <p>{r.note}</p>}
      {r && !r.note && (
        <>
          <p>
            <b>{r.refuse ? `Refused: ${r.refuse}` : `Tries ${r.routes.map((x) => x.label).join(", then ")}`}</b>
            <span className="pbx-hint"> · {r.kind}, {r.mode === "cheapest" ? "cheapest route" : r.mode === "default" ? "trunk order" : r.mode}, cap €{r.cap}/min</span>
          </p>
          <table className="users-table">
            <thead><tr><th>Route</th><th>Destination</th><th>€/min</th><th>2-min call €</th><th>Increments</th><th></th></tr></thead>
            <tbody>
              {r.candidates.map((c) => (
                <tr key={c.label} className={c.dropped ? "pbx-row-disabled" : ""}>
                  <td>{c.label}</td>
                  <td>{c.destination ? `${c.prefix} ${c.destination}` : "—"}</td>
                  <td>{c.per_min ?? "—"}</td>
                  <td>{c.typical ?? "—"}</td>
                  <td>{c.increments || "—"}{c.conn_fee && Number(c.conn_fee) ? ` + ${c.conn_fee} ${c.currency}` : ""}</td>
                  <td>{c.dropped || (c.expired ? "rate sheet expired" : "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="pbx-hint">&ldquo;Cheapest&rdquo; orders by the 2-minute call, so increments and connection fees count. Prices are ex VAT, in euro at the latest ECB rate.</p>
        </>
      )}
      {data?.expired_sheets?.length > 0 && (
        <div className="ticket-action-result ticket-action-result-error">
          Rate sheet expired for {data.expired_sheets.map((x) => `${x.label} (${x.expired.slice(0, 10)})`).join(", ")}.
          Its latest prices are still used, so the price cap won&rsquo;t see any rise until you upload the newer sheet
          in Billing.
        </div>
      )}
      {stale && (
        <div className="ticket-action-result ticket-action-result-error">
          Outbound calls have gone out since the dashboard started without a route check. Check that Asterisk&rsquo;s
          dialplan was reloaded and func_curl is running.
        </div>
      )}
      <div className="ticket-action-label pbx-gap">
        Recent route checks {data?.last_check ? `(last ${new Date(data.last_check).toLocaleString()})` : "(none since the dashboard started)"}
      </div>
      {data?.recent?.length ? (
        <table className="users-table">
          <thead><tr><th>When</th><th>Type</th><th>Number</th><th>Answer</th><th>Took</th></tr></thead>
          <tbody>
            {data.recent.map((x, i) => (
              <tr key={i}><td>{new Date(x.at).toLocaleTimeString()}</td><td>{x.kind}</td><td>+{x.number}</td>
                <td>{x.reply.startsWith("OK:") ? x.routes.join(" → ") : x.reply.replace("REFUSE:", "Refused: ")}</td><td>{x.ms} ms</td></tr>
            ))}
          </tbody>
        </table>
      ) : <p className="pbx-hint">None yet.</p>}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Inbound routing & ring groups
// ---------------------------------------------------------------------------

function DestinationSelect({ config, value, onChange }) {
  return (
    <select className="idea-select pbx-select" value={value} onChange={(e) => onChange(e.target.value)}>
      <optgroup label="Ring groups">
        {config.ring_groups.map((g) => <option key={g.number} value={g.number}>{g.number} · {g.name}</option>)}
      </optgroup>
      <optgroup label="Extensions">
        {config.extensions.map((e) => (
          <option key={e.ext} value={e.ext}>{e.ext} · {e.name}{e.enabled ? "" : " (disabled)"}</option>
        ))}
      </optgroup>
      <optgroup label="IVR menus">
        {config.ivr_menus.map((m) => <option key={m.number} value={m.number}>{m.number} · {m.name}</option>)}
      </optgroup>
      <optgroup label="Business hours">
        {config.business_hours.map((h) => <option key={h.number} value={h.number}>{h.number} · {h.name}</option>)}
      </optgroup>
    </select>
  );
}

const EMPTY_GROUP = { number: "", name: "", strategy: "ringall", ring_time: 25, members: [], voicemail: "" };

function InboundSection({ config, send, busy }) {
  const [route, setRoute] = useState({ did: "", label: "", target: config.settings.inbound_default });
  const [group, setGroup] = useState(null); // null | { ...fields, isNew }
  const nameOf = (n) =>
    config.ring_groups.find((g) => g.number === n)?.name || config.extensions.find((e) => e.ext === n)?.name ||
    config.ivr_menus.find((m) => m.number === n)?.name || config.business_hours.find((h) => h.number === n)?.name || "?";
  const extName = (n) => config.extensions.find((e) => e.ext === n);

  const saveGroup = async () => {
    const body = { name: group.name, strategy: group.strategy, ring_time: Number(group.ring_time), members: group.members,
      voicemail: group.voicemail || "" };
    const ok = group.isNew
      ? await send("POST", "/ringgroups", { ...body, number: group.number.trim() || null }, "Ring group created.")
      : await send("PUT", `/ringgroups/${group.number}`, body, "Ring group saved.");
    if (ok) setGroup(null);
  };

  return (
    <>
      <section className="panel">
        <h2>Inbound calls</h2>
        <div className="idea-form-row pbx-add-row">
          <span className="pbx-field-inline">All other incoming calls ring</span>
          <DestinationSelect
            config={config}
            value={config.settings.inbound_default}
            onChange={(v) => send("PUT", "/settings", { ...config.settings, inbound_default: v }, "Default destination updated.")}
          />
        </div>

        <table className="users-table">
          <thead><tr><th>Number called</th><th>Label</th><th>Rings</th><th></th></tr></thead>
          <tbody>
            {config.inbound_routes.length === 0 && (
              <tr><td colSpan={4} className="panel-empty">No number-specific routes. Every call goes to the default above.</td></tr>
            )}
            {config.inbound_routes.map((r) => (
              <tr key={r.did}>
                <td>+{r.did}</td>
                <td>{r.label || "—"}</td>
                <td>{r.target} · {nameOf(r.target)}</td>
                <td className="pbx-actions">
                  <button
                    className="ticket-action-btn idea-delete-btn"
                    disabled={busy}
                    onClick={() => send("DELETE", `/inbound/${r.did}`, null, "Route removed.")}
                  >Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <form
          className="idea-form-row pbx-add-row pbx-gap"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await send("POST", "/inbound", route, "Route saved.")) setRoute({ ...route, did: "", label: "" });
          }}
        >
          <input className="idea-input" placeholder="Your number, e.g. 01 234 5678" value={route.did}
            onChange={(e) => setRoute({ ...route, did: e.target.value })} />
          <input className="idea-input pbx-name-input" placeholder="Label (optional)" maxLength={40} value={route.label}
            onChange={(e) => setRoute({ ...route, label: e.target.value })} />
          <DestinationSelect config={config} value={route.target} onChange={(v) => setRoute({ ...route, target: v })} />
          <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !route.did.trim()}>Add route</button>
        </form>
      </section>

      <section className="panel pbx-gap">
        <h2>Ring groups</h2>
        <p className="pbx-hint pbx-intro">
          Ring all rings every member at once. Hunt rings them one after another, for the ring time each.
          Phones can also dial a group's number directly.
        </p>

        {config.ring_groups.length === 0 && !group && <p className="panel-empty">No ring groups yet.</p>}
        <ul className="idea-list">
          {config.ring_groups.map((g) => (
            <li key={g.number} className="idea-item">
              <div className="idea-item-head">
                <span className="idea-priority idea-priority-medium">{g.number}</span>
                <span className="idea-title">{g.name}</span>
                <span className="pbx-hint">
                  {g.strategy === "hunt" ? "Hunt" : "Ring all"} · {g.ring_time}s
                  {" · "}{g.voicemail ? `voicemail to ${g.voicemail}` : "no voicemail"}
                </span>
              </div>
              <div className="pbx-chips">
                {g.members.map((m) => (
                  <span key={m} className={`pbx-chip ${extName(m)?.enabled ? "" : "pbx-chip-off"}`}
                    title={extName(m)?.enabled ? "" : "Disabled: skipped until re-enabled"}>
                    {m} · {extName(m)?.name}
                  </span>
                ))}
              </div>
              <div className="idea-item-foot">
                <button className="ticket-action-btn" onClick={() => setGroup({ ...g, isNew: false })}>Edit</button>
                <button
                  className="ticket-action-btn idea-delete-btn"
                  disabled={busy}
                  onClick={() => window.confirm(`Delete ring group ${g.number} (${g.name})?`) &&
                    send("DELETE", `/ringgroups/${g.number}`, null, "Ring group deleted.")}
                >Delete</button>
              </div>
            </li>
          ))}
        </ul>

        {group ? (
          <div className="idea-item idea-item-editing pbx-gap">
            <div className="idea-form-row">
              {group.isNew && (
                <input className="idea-input pbx-num-input" placeholder="Number" value={group.number}
                  title={`Leave blank for the next free number (${config.limits.group_min}-${config.limits.group_max})`}
                  onChange={(e) => setGroup({ ...group, number: e.target.value })} />
              )}
              <input className="idea-input pbx-name-input" placeholder="Group name, e.g. Sales" maxLength={40}
                value={group.name} onChange={(e) => setGroup({ ...group, name: e.target.value })} />
              <select className="idea-select pbx-select" value={group.strategy}
                onChange={(e) => setGroup({ ...group, strategy: e.target.value })}>
                <option value="ringall">Ring all</option>
                <option value="hunt">Hunt (one by one)</option>
              </select>
              <label className="idea-field">
                Ring time
                <input className="idea-input pbx-num-input" type="number" min={5} max={120} value={group.ring_time}
                  onChange={(e) => setGroup({ ...group, ring_time: e.target.value })} />
                s
              </label>
            </div>
            <div className="ticket-action-label pbx-gap">Members{group.strategy === "hunt" ? " (rung in the order ticked)" : ""}</div>
            <div className="pbx-member-grid">
              {config.extensions.map((e) => (
                <label key={e.ext} className="pbx-toggle">
                  <input
                    type="checkbox"
                    checked={group.members.includes(e.ext)}
                    onChange={(ev) => setGroup({
                      ...group,
                      members: ev.target.checked ? [...group.members, e.ext] : group.members.filter((m) => m !== e.ext),
                    })}
                  />
                  {e.ext} · {e.name}
                </label>
              ))}
            </div>
            <label className="pbx-field pbx-gap">
              <span>If nobody answers, leave a voicemail in</span>
              <select className="idea-select pbx-select" value={group.voicemail || ""}
                onChange={(e) => setGroup({ ...group, voicemail: e.target.value })}>
                <option value="">No voicemail (the call ends after the ring time)</option>
                {config.extensions.map((e) => <option key={e.ext} value={e.ext}>{e.ext} · {e.name}</option>)}
              </select>
            </label>
            <div className="ticket-action-buttons">
              <button className="ticket-action-btn" onClick={() => setGroup(null)}>Cancel</button>
              <button className="ticket-action-btn ticket-action-btn-primary"
                disabled={busy || !group.name.trim() || group.members.length === 0} onClick={saveGroup}>
                {group.isNew ? "Create group" : "Save group"}
              </button>
            </div>
          </div>
        ) : (
          <button className="ticket-action-btn ticket-action-btn-primary pbx-gap" onClick={() => setGroup({ ...EMPTY_GROUP, isNew: true })}>
            New ring group
          </button>
        )}
      </section>
    </>
  );
}

// Which destinations each number type can call right now, under the saved
// routing, ticks and caps, from the current rate sheets.
const COVERAGE_STATUS = { available: "Available", partly: "Partly", unavailable: "Unavailable" };

const NO_FILTERS = { destination: "", status: "", prefix: "", route: "", maxPrice: "", why: "" };

function Coverage({ apiBase, numberTypes }) {
  const [kind, setKind] = useState("international");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [f, setF] = useState(NO_FILTERS);

  const load = async () => {
    setLoading(true);
    setError("");
    const res = await fetch(`${apiBase}/api/pbx/coverage?kind=${kind}`);
    const d = await res.json().catch(() => ({}));
    setLoading(false);
    if (res.ok) setData(d); else setError(d.detail || `Error ${res.status}`);
  };

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const has = (text, q) => text.toLowerCase().includes(q.trim().toLowerCase());
  const prefix = f.prefix.trim().replace(/^\+/, "").replace(/\s/g, "");
  const max = parseFloat(f.maxPrice.replace(",", "."));
  const rows = (data?.rows || []).filter((r) =>
    (!f.destination.trim() || has(r.destination, f.destination))
    && (!f.status || (f.status === "blocked" ? r.status !== "available" : r.status === f.status))
    && (!prefix || r.all_prefixes.some((p) => p.startsWith(prefix)))
    && (!f.route || r.routes.includes(f.route))
    && (Number.isNaN(max) || (r.per_min != null && parseFloat(r.per_min) <= max))
    && (!f.why.trim() || has(r.why.join(" "), f.why)));
  const routes = [...new Set((data?.rows || []).flatMap((r) => r.routes))].sort();
  const filtered = JSON.stringify(f) !== JSON.stringify(NO_FILTERS);
  const LIMIT = 400;

  return (
    <section className="panel pbx-gap">
      <h2>Coverage</h2>
      <div className="idea-form-row">
        <select className="idea-select pbx-select" value={kind} onChange={(e) => { setKind(e.target.value); setData(null); }}>
          {Object.entries(numberTypes).map(([k, label]) => <option key={k} value={k}>{label.split(" (")[0]}</option>)}
        </select>
        <button className="ticket-action-btn ticket-action-btn-primary" disabled={loading} onClick={load}>
          {loading ? "Working it out…" : data ? "Refresh" : "Show coverage"}
        </button>
        {data && (
          <a className="ticket-action-btn" href={`${apiBase}/api/pbx/coverage.csv?kind=${kind}`}>Download every prefix (CSV)</a>
        )}
        <span className="pbx-hint">Uses the saved routing, ticks and caps, and the latest rate sheets: the same rules as real calls.</span>
      </div>
      {error && <div className="ticket-action-result ticket-action-result-error">{error}</div>}
      {data && (
        <>
          <p>
            <b>{data.totals.available} available</b>, {data.totals.partly} partly, <b>{data.totals.unavailable} unavailable</b>
            <span className="pbx-hint">
              {" "}· {data.prefixes.toLocaleString()} prefixes · {data.mode === "cheapest" ? "cheapest route" : data.mode === "default" ? "trunk order" : data.mode}
              {data.routes.length ? ` via ${data.routes.join(", ")}` : ""} · cap €{data.cap}/min
            </span>
          </p>
          <div className="idea-form-row">
            <span className="pbx-hint">
              {rows.length.toLocaleString()} of {data.rows.length.toLocaleString()} destinations
              {rows.length > LIMIT ? ` (first ${LIMIT} shown; filter to narrow)` : ""}
            </span>
            {filtered && <button className="ticket-action-btn" onClick={() => setF(NO_FILTERS)}>Clear filters</button>}
          </div>
          <table className="users-table pbx-coverage">
            <thead>
              <tr><th>Destination</th><th>Status</th><th>Prefixes</th><th>Route</th><th>€/min</th><th>Why not</th></tr>
              <tr className="pbx-filter-row">
                <th><input className="idea-input" placeholder="Filter" value={f.destination} onChange={set("destination")} /></th>
                <th>
                  <select className="idea-select" value={f.status} onChange={set("status")}>
                    <option value="">All</option>
                    <option value="available">Available</option>
                    <option value="partly">Partly</option>
                    <option value="unavailable">Unavailable</option>
                    <option value="blocked">Not fully available</option>
                  </select>
                </th>
                <th><input className="idea-input" placeholder="Starts with" value={f.prefix} onChange={set("prefix")} /></th>
                <th>
                  <select className="idea-select" value={f.route} onChange={set("route")}>
                    <option value="">All</option>
                    {routes.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </th>
                <th><input className="idea-input" placeholder="Up to" inputMode="decimal" value={f.maxPrice} onChange={set("maxPrice")} /></th>
                <th><input className="idea-input" placeholder="Filter" value={f.why} onChange={set("why")} /></th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, LIMIT).map((r) => (
                <tr key={r.destination}>
                  <td>{r.destination}</td>
                  <td><span className={`pbx-bill-status pbx-bill-${r.status === "available" ? "rated" : r.status === "partly" ? "provisional" : "unrated"}`}>
                    {COVERAGE_STATUS[r.status]}</span></td>
                  <td title={r.all_prefixes.slice(0, 20).map((p) => `+${p}`).join(", ") + (r.all_prefixes.length > 20 ? "…" : "")}>
                    {r.available}{r.blocked ? ` of ${r.available + r.blocked}` : ""}
                    <span className="pbx-hint"> (+{r.sample[0]}{r.available + r.blocked > 1 ? "…" : ""})</span>
                  </td>
                  <td>{r.routes.join(", ") || "—"}</td>
                  <td>{r.per_min ?? "—"}</td>
                  <td className="pbx-hint">{r.why.join("; ")}</td>
                </tr>
              ))}
              {!rows.length && <tr><td colSpan={6} className="panel-empty">No destinations match these filters.</td></tr>}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
