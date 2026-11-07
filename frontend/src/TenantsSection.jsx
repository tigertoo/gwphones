import React, { useCallback, useEffect, useState } from "react";

// Tenants: see backend/pbx.py (Tenants) and the switcher in PbxPanel. The
// main tenant is today's system; every other has its own extensions,
// numbering, voicemail, recordings, park lot, caller ID and call limit.

const EMPTY = { name: "", outbound_cid: "", max_calls: 4, sip_host: "" };

export default function TenantsSection({ config, send, busy, apiBase, switchTo }) {
  const [tenants, setTenants] = useState(null);
  const [suffix, setSuffix] = useState(""); // PBX_TENANT_HOST_SUFFIX, e.g. .sip.example.ie
  const [form, setForm] = useState(null); // a new tenant
  const [edit, setEdit] = useState(null); // { id, name, sip_host, status }

  const load = useCallback(async () => {
    const res = await fetch(`${apiBase}/api/pbx/tenants`);
    if (res.ok) {
      const data = await res.json();
      setTenants(data.tenants);
      setSuffix(data.host_suffix || "");
    }
  }, [apiBase]);
  useEffect(() => { load(); }, [load, config]);

  if (!tenants) return <section className="panel"><p className="panel-empty">Loading…</p></section>;
  const hostExample = suffix ? `acme${suffix}` : "acme.sip.example.ie";

  const create = async () => {
    const data = await send("POST", "/tenants", { ...form, max_calls: Number(form.max_calls) },
      `Tenant ${form.name} created.`);
    if (data) { setForm(null); load(); }
  };
  const save = async (t, changes, text) => {
    if (await send("PUT", `/tenants/${t.id}`, { name: t.name, sip_host: t.sip_host, status: t.status, ...changes }, text)) {
      setEdit(null);
      load();
    }
  };

  return (
    <section className="panel">
      <h2>Tenants</h2>
      <p className="pbx-hint pbx-intro">
        Each tenant has its own extensions (its own 201, 600, 500…), voicemail, recordings, park lot, caller ID and limit
        on calls at once, and can&rsquo;t reach another&rsquo;s. Choose one with Tenant at the top to work on it. A
        suspended tenant can&rsquo;t call out (112 and 999 still work) and its incoming calls go to its main mailbox.
      </p>
      <table className="users-table">
        <thead><tr><th>Code</th><th>Name</th><th>Extensions</th><th>Caller ID</th><th>Calls at once</th><th>SIP hostname</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {tenants.map((t) => (edit?.id === t.id ? (
            <tr key={t.id}>
              <td>{t.code}</td>
              <td><input className="idea-input pbx-name-input" maxLength={40} value={edit.name}
                onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></td>
              <td>{t.extensions}</td>
              <td colSpan={2} className="pbx-hint">Set under Trunks &amp; Outbound while working on it</td>
              <td><input className="idea-input" placeholder={hostExample} value={edit.sip_host}
                onChange={(e) => setEdit({ ...edit, sip_host: e.target.value })} /></td>
              <td>{t.status}</td>
              <td className="pbx-actions">
                <button className="ticket-action-btn" onClick={() => setEdit(null)}>Cancel</button>
                <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !edit.name.trim()}
                  onClick={() => save(t, { name: edit.name, sip_host: edit.sip_host }, "Tenant saved.")}>Save</button>
              </td>
            </tr>
          ) : (
            <tr key={t.id}>
              <td>{t.code}</td>
              <td>{t.name}{t.main && <span className="pbx-hint"> (main)</span>}
                {t.id === config.tenant.id && <span className="pbx-chip">working on it</span>}</td>
              <td>{t.extensions}</td>
              <td>{t.settings.outbound_cid || <span className="pbx-hint">none</span>}</td>
              <td>{t.settings.max_outbound}{t.main && <span className="pbx-hint"> out</span>}</td>
              <td>{t.sip_host || <span className="pbx-hint">—</span>}</td>
              <td>{t.status === "active" ? "Active" : <span className="pbx-tenant-flag">Suspended</span>}</td>
              <td className="pbx-actions">
                {t.id !== config.tenant.id && (
                  <button className="ticket-action-btn" onClick={() => switchTo(t.id)}>Work on it</button>
                )}
                <button className="ticket-action-btn" onClick={() => setEdit({ id: t.id, name: t.name, sip_host: t.sip_host })}>Edit</button>
                {!t.main && (t.status === "active" ? (
                  <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                    onClick={() => window.confirm(`Suspend ${t.name}? Its calls out stop (112 and 999 still work) and calls in go to its main mailbox.`)
                      && save(t, { status: "suspended" }, `${t.name} suspended.`)}>Suspend</button>
                ) : (
                  <button className="ticket-action-btn" disabled={busy}
                    onClick={() => save(t, { status: "active" }, `${t.name} active again.`)}>Reactivate</button>
                ))}
              </td>
            </tr>
          )))}
        </tbody>
      </table>

      {form ? (
        <div className="idea-item idea-item-editing pbx-gap">
          <div className="idea-form-row">
            <input className="idea-input pbx-name-input" placeholder="Name, e.g. Acme Ltd" maxLength={40} value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <input className="idea-input" placeholder="Caller ID, e.g. +35312345678" value={form.outbound_cid}
              onChange={(e) => setForm({ ...form, outbound_cid: e.target.value })} />
            <label className="idea-field">
              Calls at once
              <input className="idea-input pbx-num-input" type="number" min={1} max={30} value={form.max_calls}
                onChange={(e) => setForm({ ...form, max_calls: e.target.value })} />
            </label>
            <input className="idea-input" placeholder={`SIP hostname, e.g. ${hostExample}`} value={form.sip_host}
              onChange={(e) => setForm({ ...form, sip_host: e.target.value })} />
          </div>
          <p className="pbx-hint">
            The caller ID is what its calls show, and its emergency calls when an extension has none of its own: an Irish
            number of the tenant&rsquo;s. Calls at once counts calls in and out together. It gets the next tenant code;
            add its extensions, numbers and menus by choosing it at the top. With a SIP hostname, its phones are set
            up to register to it (so it can move to another server by DNS); without one, to this server&rsquo;s.
          </p>
          <div className="ticket-action-buttons">
            <button className="ticket-action-btn" onClick={() => setForm(null)}>Cancel</button>
            <button className="ticket-action-btn ticket-action-btn-primary"
              disabled={busy || !form.name.trim() || !form.outbound_cid.trim()} onClick={create}>Create tenant</button>
          </div>
        </div>
      ) : (
        <button className="ticket-action-btn ticket-action-btn-primary pbx-gap" onClick={() => setForm(EMPTY)}>New tenant</button>
      )}
    </section>
  );
}
