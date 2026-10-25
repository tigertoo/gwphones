import React, { useEffect, useState, useCallback } from "react";

// Phone System tab: manages the Asterisk PBX on this VPS through the
// dashboard's own /api/pbx/* endpoints (see backend/pbx.py). Every save is
// applied to Asterisk straight away; calls in progress are not dropped.

const SECTIONS = [
  { key: "status", label: "Status" },
  { key: "extensions", label: "Extensions" },
  { key: "trunks", label: "Trunks & Outbound" },
  { key: "inbound", label: "Inbound & Ring Groups" },
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

export default function PbxPanel({ apiBase }) {
  const [section, setSection] = useState("status");
  const [config, setConfig] = useState(null);
  const [status, setStatus] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [result, setResult] = useState(null); // { ok, text } from the last save
  const [busy, setBusy] = useState(false);

  const loadConfig = useCallback(async () => {
    try {
      const res = await fetch(`${apiBase}/api/pbx/config`);
      if (!res.ok) throw new Error();
      setConfig(await res.json());
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
    try {
      const res = await fetch(`${apiBase}/api/pbx${path}`, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setResult({ ok: false, text: typeof data.detail === "string" ? data.detail : "Something went wrong." });
        return null;
      }
      const applied = data.ok !== false;
      setResult({
        ok: applied,
        text: applied ? successText || "Saved and applied." : (data.messages || []).join(" "),
      });
      await loadConfig();
      loadStatus();
      return data;
    } catch {
      setResult({ ok: false, text: "Connection error. Please try again." });
      return null;
    } finally {
      setBusy(false);
    }
  };

  if (loadError) return <section className="panel"><div className="admin-dash-error">{loadError}</div></section>;
  if (!config) return <section className="panel"><p className="panel-empty">Loading…</p></section>;

  const props = { config, status, send, busy, apiBase };

  return (
    <div className="pbx">
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

      {section === "status" && <StatusSection {...props} />}
      {section === "extensions" && <ExtensionsSection {...props} />}
      {section === "trunks" && <TrunksSection {...props} />}
      {section === "inbound" && <InboundSection {...props} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

function StatusSection({ config, status }) {
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

  return (
    <>
      {status.last_apply?.ok === false && (
        <div className="admin-dash-error">Last change wasn't applied: {status.last_apply.messages.join(" ")}</div>
      )}

      <section className="pbx-trunk-grid">
        {settings.trunk_order.map((key) => {
          const s = status.trunks[key] || {};
          const role = enabledOrder.indexOf(key);
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
                Outbound: {role === -1 ? "off" : role === 0 ? "primary" : "fallback"}
              </div>
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
              <tr><th>Channel</th><th>Caller</th><th>Dialled</th><th>State</th><th>Duration</th></tr>
            </thead>
            <tbody>
              {status.channels.map((c) => (
                <tr key={c.channel}>
                  <td>{c.channel}</td>
                  <td>{c.caller_id || "—"}</td>
                  <td>{c.exten || "—"}</td>
                  <td>{c.state}</td>
                  <td>{formatDuration(c.duration_seconds)}</td>
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
    how: "Install Linphone, choose \u201cFetch remote configuration\u201d (or the QR code option) on its setup screen and scan this from there, not with the Camera app. Linphone won't ring while it's closed: it can't get push notifications from this server.",
  },
};

function SetupQr({ ext, apiBase }) {
  const [link, setLink] = useState(null); // { app, url, expires_at, qr }
  const [error, setError] = useState("");

  const make = async (app) => {
    setError("");
    const res = await fetch(`${apiBase}/api/pbx/extensions/${ext}/setup-link`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    </div>
  );
}

function ExtensionsSection({ config, status, send, busy, apiBase }) {
  const [draft, setDraft] = useState({ ext: "", name: "" });
  const [editing, setEditing] = useState(null); // { ext, name }
  const [creds, setCreds] = useState(null); // { ext, ...credentials }
  const { limits } = config;

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
                    <SetupQr key={e.ext} ext={e.ext} apiBase={apiBase} />
                    <div className="ticket-action-label">Or enter these in the phone app for extension {e.ext}:</div>
                    <CopyField label="Server" value={creds.server} />
                    <CopyField label="Port" value={creds.port} />
                    <CopyField label="Transport" value={creds.transport} />
                    <CopyField label="Encryption" value={creds.media_encryption} />
                    <CopyField label="Username" value={creds.username} />
                    <CopyField label="Password" value={creds.password} />
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
// Trunks & outbound
// ---------------------------------------------------------------------------

function TrunksSection({ config, status, send, busy }) {
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
    <section className="panel">
      <h2>Outbound trunks</h2>
      <p className="pbx-hint pbx-intro">
        Calls go out through the first enabled trunk. The next one is only tried if that trunk can't take the call
        (down, refused or congested). 112/999 always go via Irish VoIP, whatever is set here.
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
              <button type="button" className="ticket-action-btn" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up">↑</button>
              <button type="button" className="ticket-action-btn" disabled={i === form.trunk_order.length - 1} onClick={() => move(i, 1)} aria-label="Move down">↓</button>
            </li>
          );
        })}
      </ol>
      {noneEnabled && <div className="ticket-action-result ticket-action-result-error">With no trunk enabled, only extension-to-extension and emergency calls will work.</div>}

      <div className="pbx-fields">
        <label className="pbx-field">
          <span>Caller ID shown to people you call</span>
          <input
            className="idea-input"
            placeholder="+35312345678"
            value={form.outbound_cid}
            onChange={(e) => setForm({ ...form, outbound_cid: e.target.value })}
          />
          <small className="pbx-hint">Use a number on your Irish VoIP account. Carriers can reject calls showing a number you don't own.</small>
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
          <small className="pbx-hint">A brake on toll fraud: extra calls get a busy tone.</small>
        </label>
      </div>

      <div className="ticket-action-buttons">
        <button className="ticket-action-btn" disabled={!dirty || busy} onClick={() => setForm(config.settings)}>Undo changes</button>
        <button
          className="ticket-action-btn ticket-action-btn-primary"
          disabled={!dirty || busy}
          onClick={() => send("PUT", "/settings", form, "Trunk settings saved and applied.")}
        >Save</button>
      </div>
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
    </select>
  );
}

const EMPTY_GROUP = { number: "", name: "", strategy: "ringall", ring_time: 25, members: [] };

function InboundSection({ config, send, busy }) {
  const [route, setRoute] = useState({ did: "", label: "", target: config.settings.inbound_default });
  const [group, setGroup] = useState(null); // null | { ...fields, isNew }
  const nameOf = (n) =>
    config.ring_groups.find((g) => g.number === n)?.name || config.extensions.find((e) => e.ext === n)?.name || "?";
  const extName = (n) => config.extensions.find((e) => e.ext === n);

  const saveGroup = async () => {
    const body = { name: group.name, strategy: group.strategy, ring_time: Number(group.ring_time), members: group.members };
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
                <span className="pbx-hint">{g.strategy === "hunt" ? "Hunt" : "Ring all"} · {g.ring_time}s</span>
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
