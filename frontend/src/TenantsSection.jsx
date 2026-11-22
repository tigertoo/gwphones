import React, { useCallback, useEffect, useState } from "react";

// Tenants: see backend/pbx.py (Tenants) and the switcher in PbxPanel. The
// main tenant is today's system; every other has its own extensions,
// numbering, voicemail, recordings, park lot, caller ID and call limit.

// A tenant's own SIP hostname, suggested from its name: "Acme Plumbing Ltd"
// is acme-plumbing-ltd.sip.example.ie, with -2, -3... if another tenant has it.
function suggestHost(name, suffix, taken) {
  if (!suffix) return "";
  const slug = name.toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 30).replace(/-+$/, "");
  if (!slug) return "";
  for (let n = 1; n < 100; n += 1) {
    const host = `${slug}${n > 1 ? `-${n}` : ""}${suffix}`;
    if (!taken.has(host)) return host;
  }
  return "";
}

const EMPTY = { name: "", outbound_cid: "", max_calls: 4, sip_host: "", web_max: 10, video: false, video_price: "5.00", ext_digits: "4" };

export default function TenantsSection({ config, send, busy, apiBase, switchTo }) {
  const [tenants, setTenants] = useState(null);
  const [suffix, setSuffix] = useState(""); // PBX_TENANT_HOST_SUFFIX, e.g. .sip.example.ie
  const [form, setForm] = useState(null); // a new tenant
  const [edit, setEdit] = useState(null); // { id, name, sip_host, status }
  const [portalFor, setPortalFor] = useState(null); // a tenant id: its portal people shown

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
    const { hostEdited, admin_email, admin_name, ...body } = form; // eslint-disable-line no-unused-vars
    const data = await send("POST", "/tenants", { ...body, max_calls: Number(body.max_calls), web_max: Number(body.web_max) },
      `Tenant ${form.name} created.`);
    if (data) {
      setForm(null);
      load();
      // its first portal admin, invited straight away
      if (admin_email && admin_email.trim()) {
        await send("POST", `/tenants/${data.id}/portal-admins`, { email: admin_email.trim(), name: admin_name || "" },
          `${form.name} created, and ${admin_email.trim()} invited to its portal.`);
        setPortalFor(data.id);
      }
    }
  };
  const save = async (t, changes, text) => {
    if (await send("PUT", `/tenants/${t.id}`, { name: t.name, sip_host: t.sip_host, status: t.status, ...changes }, text)) {
      setEdit(null);
      load();
    }
  };

  // Deleting: only a suspended tenant with no numbers left, its code typed again.
  const remove = async (t) => {
    const typed = window.prompt(`Delete ${t.name} (${t.code})? Its extensions, groups, menus, desk phones, sites and settings `
      + "are removed, and its portal logins. Its recordings, voicemail and greetings are kept 30 days, then removed. "
      + "Its billing statements and call records are kept. This can't be undone.\n\nType its code to delete it:");
    if (typed === null) return;
    const data = await send("DELETE", `/tenants/${t.id}`, { confirm: typed.trim() }, `${t.name} deleted.`);
    if (data) load();
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
        <thead><tr><th>Code</th><th>Name</th><th>Extensions</th><th>Caller ID</th><th>Calls at once</th><th>Browser calls</th><th>Video</th><th>SIP hostname</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {tenants.map((t) => (edit?.id === t.id ? (
            <tr key={t.id}>
              <td>{t.code}</td>
              <td><input className="idea-input pbx-name-input" maxLength={40} value={edit.name}
                onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></td>
              <td>{t.extensions}<span className="pbx-hint"> · {t.settings.ext_digits || "3"} digits</span></td>
              <td colSpan={2} className="pbx-hint">Set under Trunks &amp; Outbound while working on it</td>
              <td><input className="idea-input pbx-num-input" type="number" min={1} max={50} value={edit.web_max}
                onChange={(e) => setEdit({ ...edit, web_max: e.target.value })} /></td>
              <td><input type="checkbox" checked={edit.video} aria-label="Video add-on"
                onChange={(e) => setEdit({ ...edit, video: e.target.checked })} />
                {!t.main && <input className="idea-input pbx-num-input" aria-label="Video add-on, € a month" value={edit.video_price}
                  onChange={(e) => setEdit({ ...edit, video_price: e.target.value })} title="€ a month on its statement" />}</td>
              <td><input className="idea-input" placeholder={hostExample} value={edit.sip_host}
                onChange={(e) => setEdit({ ...edit, sip_host: e.target.value })} /></td>
              <td>{t.status}</td>
              <td className="pbx-actions">
                <button className="ticket-action-btn" onClick={() => setEdit(null)}>Cancel</button>
                <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !edit.name.trim()}
                  onClick={() => save(t, { name: edit.name, sip_host: edit.sip_host, web_max: Number(edit.web_max), video: edit.video,
                    ...(t.main ? {} : { video_price: edit.video_price }) }, "Tenant saved.")}>Save</button>
              </td>
            </tr>
          ) : (
            <React.Fragment key={t.id}><tr>
              <td>{t.code}</td>
              <td>{t.name}{t.main && <span className="pbx-hint"> (main)</span>}
                {t.id === config.tenant.id && <span className="pbx-chip">working on it</span>}</td>
              <td>{t.extensions}<span className="pbx-hint"> · {t.settings.ext_digits || "3"} digits</span></td>
              <td>{t.settings.outbound_cid || <span className="pbx-hint">none</span>}</td>
              <td>{t.settings.max_outbound}{t.main && <span className="pbx-hint"> out</span>}</td>
              <td>{t.settings.web_max}</td>
              <td>{t.settings.video === "1" ? (t.main ? "Yes" : `Yes, €${t.settings.video_price}/month`) : "—"}</td>
              <td>{t.sip_host || <span className="pbx-hint">—</span>}</td>
              <td>{t.status === "active" ? "Active" : <span className="pbx-tenant-flag">Suspended</span>}</td>
              <td className="pbx-actions">
                {t.id !== config.tenant.id && (
                  <button className="ticket-action-btn" onClick={() => switchTo(t.id)}>Work on it</button>
                )}
                {!t.main && <button className="ticket-action-btn" onClick={() => setPortalFor(portalFor === t.id ? null : t.id)}>
                  Portal</button>}
                <button className="ticket-action-btn" onClick={() => setEdit({ id: t.id, name: t.name, sip_host: t.sip_host, web_max: t.settings.web_max,
                  video: t.settings.video === "1", video_price: t.settings.video_price })}>Edit</button>
                {!t.main && (t.status === "active" ? (
                  <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                    onClick={() => window.confirm(`Suspend ${t.name}? Its calls out stop (112 and 999 still work) and calls in go to its main mailbox.`)
                      && save(t, { status: "suspended" }, `${t.name} suspended.`)}>Suspend</button>
                ) : (
                  <>
                    <button className="ticket-action-btn" disabled={busy}
                      onClick={() => save(t, { status: "active" }, `${t.name} active again.`)}>Reactivate</button>
                    <button className="ticket-action-btn idea-delete-btn" disabled={busy} onClick={() => remove(t)}
                      title="Remove its numbers first">Delete</button>
                  </>
                ))}
              </td>
            </tr>
            {portalFor === t.id && (
              <tr><td colSpan={11}><PortalPeople tenant={t} apiBase={apiBase} send={send} busy={busy} /></td></tr>
            )}
            </React.Fragment>
          )))}
        </tbody>
      </table>

      {form ? (
        <div className="idea-item idea-item-editing pbx-gap">
          <div className="idea-form-row">
            <input className="idea-input pbx-name-input" placeholder="Name, e.g. Acme Ltd" maxLength={40} value={form.name}
              onChange={(e) => {
                const name = e.target.value;
                // the suggestion follows the name until the address is edited by hand
                const taken = new Set(tenants.map((t) => t.sip_host).filter(Boolean));
                setForm({ ...form, name, ...(form.hostEdited ? {} : { sip_host: suggestHost(name, suffix, taken) }) });
              }} />
            <input className="idea-input" placeholder="Caller ID, e.g. +35312345678" value={form.outbound_cid}
              onChange={(e) => setForm({ ...form, outbound_cid: e.target.value })} />
            <input className="idea-input" type="email" placeholder="Its first portal admin's email (optional)"
              value={form.admin_email || ""} onChange={(e) => setForm({ ...form, admin_email: e.target.value })}
              title="Invited by email as soon as the tenant is made: they choose a password and set up two-factor codes" />
            <label className="idea-field">
              Extensions
              <select className="idea-input" value={form.ext_digits} onChange={(e) => setForm({ ...form, ext_digits: e.target.value })}>
                <option value="4">4 digits (2000–8999, room for sites)</option>
                <option value="3">3 digits (200–299, a small office)</option>
              </select>
            </label>
            <label className="idea-field">
              Calls at once
              <input className="idea-input pbx-num-input" type="number" min={1} max={30} value={form.max_calls}
                onChange={(e) => setForm({ ...form, max_calls: e.target.value })} />
            </label>
            <label className="idea-field">
              Browser calls
              <input className="idea-input pbx-num-input" type="number" min={1} max={50} value={form.web_max}
                onChange={(e) => setForm({ ...form, web_max: e.target.value })} />
            </label>
            <label className="pbx-toggle">
              <input type="checkbox" checked={form.video} onChange={(e) => setForm({ ...form, video: e.target.checked })} />
              Video add-on
            </label>
            {form.video && (
              <label className="idea-field">
                € a month
                <input className="idea-input pbx-num-input" value={form.video_price}
                  onChange={(e) => setForm({ ...form, video_price: e.target.value })} />
              </label>
            )}
            <input className="idea-input" placeholder={`SIP hostname, e.g. ${hostExample}`} value={form.sip_host}
              onChange={(e) => setForm({ ...form, sip_host: e.target.value, hostEdited: true })}
              title="Its phones register here (a name under the wildcard, so it can be moved to another server by DNS). Empty: the server's own address." />
          </div>
          <p className="pbx-hint">
            The caller ID is what its calls show, and its emergency calls when an extension has none of its own: an Irish
            number of the tenant&rsquo;s. Calls at once counts calls in and out together: its numbers&rsquo; calls, the ones that
            use a carrier (its plan). Browser calls (call links) don&rsquo;t count in it: they have their own limit, there to
            protect the server and other tenants since call links are public. Video (the paid add-on) gives its
            extensions video calls and lets its call links allow the visitor&rsquo;s camera; without it, audio only. It gets the next tenant code;
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

// A tenant's portal people (from the portal, on this server), and inviting
// an admin: the invitation emailed, or the link shown to pass on by hand.
function PortalPeople({ tenant, apiBase, send, busy }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [f, setF] = useState({ email: "", name: "" });
  const [link, setLink] = useState(null);
  const load = useCallback(async () => {
    const res = await fetch(`${apiBase}/api/pbx/tenants/${tenant.id}/portal-people`);
    const body = await res.json().catch(() => ({}));
    if (res.ok) { setData(body); setError(null); } else setError(body.detail || "The portal can't be reached.");
  }, [apiBase, tenant.id]);
  useEffect(() => { load(); }, [load]);
  const invite = async (giveLink) => {
    const r = await send("POST", `/tenants/${tenant.id}/portal-admins`, { ...f, give_link: giveLink },
      giveLink ? `${f.email} invited: pass the link on.` : `${f.email} invited: the invitation is on its way.`);
    if (r) { setLink(r.link || null); setF({ email: "", name: "" }); load(); }
  };
  return (
    <div className="pbx-gap">
      <strong>{tenant.name}&rsquo;s portal</strong>
      {error && <p className="pbx-hint">{error}</p>}
      {data && (data.people.length === 0 ? <p className="pbx-hint">Nobody yet: invite its first admin.</p> : (
        <table className="users-table">
          <tbody>
            {data.people.map((p) => (
              <tr key={p.email}>
                <td>{p.name || p.email}{p.name && <span className="pbx-hint"> {p.email}</span>}</td>
                <td>{p.role === "admin" ? "Admin" : `Staff, ext ${p.ext || "—"}`}</td>
                <td>{p.disabled ? "Disabled" : p.invited ? "Invited, not accepted yet" : p.microsoft ? "Microsoft 365" : "Active"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ))}
      <div className="idea-form-row pbx-gap">
        <input className="idea-input" type="email" placeholder="Admin's email" value={f.email}
          onChange={(e) => setF({ ...f, email: e.target.value })} />
        <input className="idea-input pbx-name-input" placeholder="Name (optional)" value={f.name}
          onChange={(e) => setF({ ...f, name: e.target.value })} />
        <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !f.email.trim()}
          onClick={() => invite(false)}>Invite an admin</button>
        <button className="ticket-action-btn" disabled={busy || !f.email.trim()} onClick={() => invite(true)}
          title="Don't email it: show the link here to pass on yourself">Get the link instead</button>
      </div>
      {link && <p className="pbx-hint">Their invitation link (works once, for 72 hours): <code>{link}</code></p>}
      <p className="pbx-hint">An admin manages the tenant&rsquo;s phone system in the portal and invites everyone else from its Staff page.
        They set up two-factor codes when they first sign in.{data && !data.mail_ready && " The portal can't send email: use the link."}</p>
    </div>
  );
}
