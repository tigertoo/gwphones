import React, { useCallback, useEffect, useState } from "react";

// Billing → monthly summary, customers and their phone numbers (DDIs).
// Customers are groups of extensions; they pay your cost plus a markup
// (the default or their own), for calls and for their numbers.

const EMPTY_CUSTOMER = { name: "", email: "", markup_pct: "", notes: "", exts: [] };

function currentMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export function MonthlySummary({ apiBase, api, month, version }) {
  const [summary, setSummary] = useState(null);
  const [open, setOpen] = useState(null); // customer id (0 = not assigned)
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    api("GET", `/summary?month=${month}`).then(setSummary).catch(() => setSummary(null));
    setOpen(null);
  }, [api, month, version]);

  const show = async (id) => {
    if (open === id) return setOpen(null);
    setOpen(id);
    setDetail(await api("GET", `/customers/${id}/month?month=${month}`).catch(() => null));
  };

  if (!summary) return null;
  return (
    <section className="panel pbx-gap">
      <h2>What each customer owes</h2>
      <div className="idea-form-row">
        <a className="ticket-action-btn" href={`${apiBase}/api/billing/summary.csv?month=${month}`}>Download summary CSV</a>
        <span className="pbx-hint">Your cost and their price, ex VAT. Click a customer for the itemised list.</span>
      </div>
      <table className="users-table">
        <thead>
          <tr><th>Customer</th><th>Markup</th><th>Calls</th><th>Minutes</th><th>Calls €</th><th>Numbers €</th><th>Cost €</th><th>Price €</th><th>Margin €</th><th></th></tr>
        </thead>
        <tbody>
          {summary.customers.flatMap((e) => {
            const id = e.customer.id ?? 0;
            const t = e.totals;
            const missing = e.counts.unrated + e.counts.provisional;
            const rows = [
              <tr key={id} className="pbx-call-row" onClick={() => show(id)}>
                <td>{e.customer.name}</td>
                <td>{e.customer.id == null ? "—" : `${e.markup_pct}%${e.customer.markup_pct == null ? " (default)" : ""}`}</td>
                <td>{t.calls}</td>
                <td>{t.minutes}</td>
                <td>{t.call_price}</td>
                <td>{t.ddi_price}</td>
                <td>{t.cost}</td>
                <td><b>{t.price}</b></td>
                <td>{t.margin}</td>
                <td>{missing > 0
                  ? <span className="pbx-bill-status pbx-bill-unrated" title="Not in the totals until priced">{missing} not priced</span>
                  : <span className="pbx-bill-status pbx-bill-rated">complete</span>}</td>
              </tr>,
            ];
            if (open === id && detail) rows.push(
              <tr key={`${id}-detail`}>
                <td colSpan={10} className="ticket-action-panel">
                  <div className="idea-form-row">
                    <a className="ticket-action-btn" href={`${apiBase}/api/billing/customers/${id}/month.csv?month=${month}`}>Download itemised CSV</a>
                    {id !== 0 && (
                      <a className="ticket-action-btn" href={`${apiBase}/api/billing/customers/${id}/draft.pdf?month=${month}`}
                        target="_blank" rel="noreferrer">Preview statement (draft PDF)</a>
                    )}
                  </div>
                  <table className="users-table">
                    <thead><tr><th>Item</th><th>When</th><th>Ext</th><th>Number</th><th>Destination</th><th>Talk</th><th>Cost €</th><th>Price €</th></tr></thead>
                    <tbody>
                      {detail.ddis.map((d) => (
                        <tr key={d.did}>
                          <td>Phone number</td><td>{month}</td><td></td><td>+{d.did}</td><td>{d.label}{d.fixed_price ? " (fixed price)" : ""}</td><td></td>
                          <td>{Number(d.cost_eur).toFixed(2)}</td><td>{Number(d.price_eur).toFixed(2)}</td>
                        </tr>
                      ))}
                      {detail.calls.map((c) => (
                        <tr key={c.id}>
                          <td>Call</td><td>{c.start.slice(5, 16)}</td><td>{c.ext}</td><td>{c.number ? `+${c.number}` : c.dialled}</td>
                          <td>{c.destination || <span className="pbx-hint">{c.reason}</span>}</td>
                          <td>{Math.floor(c.billsec / 60)}:{String(c.billsec % 60).padStart(2, "0")}</td>
                          <td>{c.cost_eur != null ? Number(c.cost_eur).toFixed(4) : "—"}</td>
                          <td>{c.price_eur != null ? Number(c.price_eur).toFixed(4) : "—"}</td>
                        </tr>
                      ))}
                      {!detail.ddis.length && !detail.calls.length && (
                        <tr><td colSpan={8} className="panel-empty">Nothing this month.</td></tr>
                      )}
                    </tbody>
                  </table>
                </td>
              </tr>
            );
            return rows;
          })}
        </tbody>
      </table>
    </section>
  );
}

export function CustomersAndNumbers({ api, run, busy, version }) {
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null); // customer form, with id or null for new
  const [markup, setMarkup] = useState("");
  const [ddi, setDdi] = useState(null); // number form

  const load = useCallback(async () => {
    const d = await api("GET", "/customers").catch(() => null);
    setData(d);
    if (d) setMarkup(d.default_markup_pct);
  }, [api]);
  useEffect(() => { load(); }, [load, version]);

  if (!data) return null;
  const extName = (e) => data.extensions.find((x) => x.ext === e)?.name || "";
  const ownerOf = (e) => data.customers.find((c) => c.exts.includes(e));

  const saveCustomer = async () => {
    const body = { ...editing, markup_pct: editing.markup_pct === "" ? null : editing.markup_pct };
    const ok = editing.id
      ? await run("PUT", `/customers/${editing.id}`, body)
      : await run("POST", "/customers", body);
    if (ok) { setEditing(null); load(); }
  };
  const saveDdi = async () => {
    const ok = await run("POST", "/ddis", { ...ddi, customer_id: Number(ddi.customer_id), to_month: ddi.to_month || null });
    if (ok) { setDdi(null); load(); }
  };

  return (
    <>
      <section className="panel pbx-gap">
        <h2>Customers</h2>
        <div className="idea-form-row">
          <label className="pbx-field-inline">
            Default markup{" "}
            <input className="idea-input pbx-num-input" value={markup} onChange={(e) => setMarkup(e.target.value)} /> %
          </label>
          <button className="ticket-action-btn" disabled={busy || markup === data.default_markup_pct}
            onClick={async () => { if (await run("PUT", "/settings", { default_markup_pct: markup })) load(); }}>Save</button>
          <button className="ticket-action-btn ticket-action-btn-primary" onClick={() => setEditing({ ...EMPTY_CUSTOMER })}>Add customer</button>
        </div>
        <table className="users-table">
          <thead><tr><th>Customer</th><th>Email</th><th>Markup</th><th>Extensions</th><th>Numbers</th><th></th></tr></thead>
          <tbody>
            {data.customers.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>{c.email || "—"}</td>
                <td>{c.markup_pct == null ? `${data.default_markup_pct}% (default)` : `${c.markup_pct}%`}</td>
                <td>{c.exts.join(", ") || "—"}</td>
                <td>{c.ddis.map((d) => `+${d}`).join(", ") || "—"}</td>
                <td className="pbx-actions">
                  <button className="ticket-action-btn" onClick={() => setEditing({ ...c, markup_pct: c.markup_pct ?? "" })}>Edit</button>
                  <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                    onClick={async () => window.confirm(`Delete ${c.name}? Their extensions' calls will show as not assigned.`)
                      && (await run("DELETE", `/customers/${c.id}`)) && load()}>Delete</button>
                </td>
              </tr>
            ))}
            {!data.customers.length && <tr><td colSpan={6} className="panel-empty">No customers yet.</td></tr>}
          </tbody>
        </table>

        {editing && (
          <div className="ticket-action-panel pbx-gap">
            <div className="ticket-action-label">{editing.id ? `Edit ${editing.name}` : "New customer"}</div>
            <div className="pbx-mapping">
              <label className="pbx-field"><span>Name *</span>
                <input className="idea-input" maxLength={80} value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} /></label>
              <label className="pbx-field"><span>Email (for statements)</span>
                <input className="idea-input" type="email" value={editing.email} onChange={(e) => setEditing({ ...editing, email: e.target.value })} /></label>
              <label className="pbx-field"><span>Markup % (blank = default {data.default_markup_pct}%)</span>
                <input className="idea-input" value={editing.markup_pct} onChange={(e) => setEditing({ ...editing, markup_pct: e.target.value })} /></label>
              <label className="pbx-field"><span>Notes</span>
                <input className="idea-input" maxLength={500} value={editing.notes} onChange={(e) => setEditing({ ...editing, notes: e.target.value })} /></label>
            </div>
            <div className="ticket-action-label">Extensions</div>
            <div className="pbx-member-grid">
              {data.extensions.map((x) => {
                const owner = ownerOf(x.ext);
                const elsewhere = owner && owner.id !== editing.id;
                return (
                  <label key={x.ext} className="pbx-toggle" title={elsewhere ? `Belongs to ${owner.name}` : ""}>
                    <input type="checkbox" disabled={elsewhere} checked={editing.exts.includes(x.ext)}
                      onChange={(e) => setEditing({
                        ...editing,
                        exts: e.target.checked ? [...editing.exts, x.ext] : editing.exts.filter((v) => v !== x.ext),
                      })} />
                    {x.ext} · {x.name}{elsewhere ? ` (${owner.name})` : ""}
                  </label>
                );
              })}
            </div>
            <div className="ticket-action-buttons">
              <button className="ticket-action-btn" onClick={() => setEditing(null)}>Cancel</button>
              <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !editing.name.trim()} onClick={saveCustomer}>
                {editing.id ? "Save" : "Add customer"}
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="panel pbx-gap">
        <h2>Phone numbers</h2>
        <div className="idea-form-row">
          <button className="ticket-action-btn ticket-action-btn-primary" disabled={!data.customers.length}
            onClick={() => setDdi({ did: "", customer_id: data.customers[0]?.id ?? "", label: "", monthly_cost: "", monthly_price: "", from_month: currentMonth(), to_month: "" })}>
            Add a number
          </button>
          <span className="pbx-hint">
            {data.customers.length ? "A number's monthly cost to you; the customer pays it plus their markup, unless you set a fixed price." : "Add a customer first."}
          </span>
        </div>
        <table className="users-table">
          <thead><tr><th>Number</th><th>Customer</th><th>Label</th><th>Cost €/month</th><th>Price €/month</th><th>Months</th><th></th></tr></thead>
          <tbody>
            {data.ddis.map((d) => (
              <tr key={d.did}>
                <td>+{d.did}</td>
                <td>{data.customers.find((c) => c.id === d.customer_id)?.name}</td>
                <td>{d.label || "—"}</td>
                <td>{d.monthly_cost}</td>
                <td>{d.monthly_price ?? "cost + markup"}</td>
                <td>{d.from_month} → {d.to_month || "ongoing"}</td>
                <td className="pbx-actions">
                  <button className="ticket-action-btn" onClick={() => setDdi({ ...d, monthly_price: d.monthly_price ?? "", to_month: d.to_month ?? "" })}>Edit</button>
                  <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                    onClick={async () => window.confirm(`Remove +${d.did} from billing?`) && (await run("DELETE", `/ddis/${d.did}`)) && load()}>Remove</button>
                </td>
              </tr>
            ))}
            {!data.ddis.length && <tr><td colSpan={7} className="panel-empty">No numbers yet.</td></tr>}
          </tbody>
        </table>

        {ddi && (
          <div className="ticket-action-panel pbx-gap">
            <div className="pbx-mapping">
              <label className="pbx-field"><span>Number *</span>
                <input className="idea-input" list="pbx-inbound-numbers" placeholder="01 234 5678" value={ddi.did}
                  onChange={(e) => setDdi({ ...ddi, did: e.target.value })} />
                <datalist id="pbx-inbound-numbers">
                  {data.inbound_numbers.map((n) => <option key={n.did} value={`+${n.did}`}>{n.label}</option>)}
                </datalist></label>
              <label className="pbx-field"><span>Customer *</span>
                <select className="idea-select pbx-select" value={ddi.customer_id} onChange={(e) => setDdi({ ...ddi, customer_id: e.target.value })}>
                  {data.customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select></label>
              <label className="pbx-field"><span>Label</span>
                <input className="idea-input" maxLength={80} value={ddi.label} onChange={(e) => setDdi({ ...ddi, label: e.target.value })} /></label>
              <label className="pbx-field"><span>Monthly cost to you, € ex VAT *</span>
                <input className="idea-input" value={ddi.monthly_cost} onChange={(e) => setDdi({ ...ddi, monthly_cost: e.target.value })} /></label>
              <label className="pbx-field"><span>Fixed monthly price, € (optional)</span>
                <input className="idea-input" placeholder="cost + markup" value={ddi.monthly_price} onChange={(e) => setDdi({ ...ddi, monthly_price: e.target.value })} /></label>
              <label className="pbx-field"><span>First month *</span>
                <input className="idea-input" type="month" value={ddi.from_month} onChange={(e) => setDdi({ ...ddi, from_month: e.target.value })} /></label>
              <label className="pbx-field"><span>Last month (blank = ongoing)</span>
                <input className="idea-input" type="month" value={ddi.to_month} onChange={(e) => setDdi({ ...ddi, to_month: e.target.value })} /></label>
            </div>
            <div className="ticket-action-buttons">
              <button className="ticket-action-btn" onClick={() => setDdi(null)}>Cancel</button>
              <button className="ticket-action-btn ticket-action-btn-primary"
                disabled={busy || !ddi.did.trim() || !ddi.monthly_cost.toString().trim() || !ddi.from_month} onClick={saveDdi}>Save number</button>
            </div>
          </div>
        )}
      </section>
    </>
  );
}

// Statements: issue (locks the month's prices), PDF, email, cancel.
export function Statements({ apiBase, api, run, busy, month, version }) {
  const [list, setList] = useState([]);
  const [settings, setSettings] = useState(null);
  const [editing, setEditing] = useState(null); // business details form
  const [customers, setCustomers] = useState([]);
  const [who, setWho] = useState(""); // "" = every customer

  useEffect(() => {
    api("GET", `/statements?month=${month}`).then((d) => setList(d.statements)).catch(() => setList([]));
    api("GET", "/statement-settings").then(setSettings).catch(() => setSettings(null));
    api("GET", "/customers").then((d) => setCustomers(d.customers)).catch(() => setCustomers([]));
  }, [api, month, version]);

  if (!settings) return null;
  const s = settings.settings;
  const ended = month < currentMonth();

  return (
    <section className="panel pbx-gap">
      <h2>Statements</h2>
      {!s.business_name && !editing && (
        <div className="ticket-action-result ticket-action-result-error">Fill in your business details before issuing statements.</div>
      )}
      <div className="idea-form-row">
        <select className="idea-select pbx-select" value={who} onChange={(e) => setWho(e.target.value)}>
          <option value="">Every customer</option>
          {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !ended || !s.business_name}
          onClick={() => window.confirm(`Issue ${who ? "a statement" : "statements"} for ${month}? This locks the prices of the calls on ${who ? "it" : "them"}.`)
            && run("POST", "/statements", { month, customer_id: who ? Number(who) : null })}>
          Issue for {month}
        </button>
        {who && (
          <a className="ticket-action-btn" href={`${apiBase}/api/billing/customers/${who}/draft.pdf?month=${month}`}
            target="_blank" rel="noreferrer">Preview draft PDF</a>
        )}
        <button className="ticket-action-btn" onClick={() => setEditing({ ...s })}>Business details</button>
        <span className="pbx-hint">
          {ended ? "Locks each customer's prices for the month. Calls read later go on a follow-up." : "Statements can be issued once the month has ended; pick a customer to preview a draft now."}
        </span>
      </div>

      {editing && (
        <div className="ticket-action-panel pbx-gap">
          <div className="ticket-action-label">Shown at the top and bottom of every statement</div>
          <div className="pbx-mapping">
            {Object.entries(settings.fields).map(([k, label]) => (
              <label key={k} className="pbx-field"><span>{label}</span>
                {["address", "payment_details", "footer"].includes(k)
                  ? <textarea className="idea-input" rows={3} value={editing[k]} onChange={(e) => setEditing({ ...editing, [k]: e.target.value })} />
                  : <input className="idea-input" value={editing[k]} onChange={(e) => setEditing({ ...editing, [k]: e.target.value })} />}
              </label>
            ))}
          </div>
          <div className="ticket-action-buttons">
            <button className="ticket-action-btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="ticket-action-btn ticket-action-btn-primary" disabled={busy || !editing.business_name.trim()}
              onClick={async () => { if (await run("PUT", "/statement-settings", editing)) setEditing(null); }}>Save</button>
          </div>
          <p className="pbx-hint">Changes apply to statements issued from now on; issued ones keep what they were sent with.</p>
        </div>
      )}

      <table className="users-table pbx-gap">
        <thead><tr><th>Number</th><th>Customer</th><th>Total €</th><th>Issued</th><th>Emailed</th><th></th></tr></thead>
        <tbody>
          {list.map((st) => (
            <tr key={st.id} className={st.cancelled_at ? "pbx-row-disabled" : ""}>
              <td>{st.number}{st.follow_up ? " (follow-up)" : ""}{st.cancelled_at ? " — cancelled" : ""}</td>
              <td>{st.customer}</td>
              <td>{st.total}</td>
              <td>{new Date(st.issued_at).toLocaleDateString()}</td>
              <td>{st.emailed_at ? `${new Date(st.emailed_at).toLocaleDateString()} to ${st.emailed_to}` : "—"}</td>
              <td className="pbx-actions">
                <a className="ticket-action-btn" href={`${apiBase}/api/billing/statements/${st.id}/pdf`} target="_blank" rel="noreferrer">PDF</a>
                {!st.cancelled_at && (
                  <>
                    <button className="ticket-action-btn" disabled={busy || !settings.email_ready}
                      title={settings.email_ready ? "" : "Email isn't set up"}
                      onClick={() => {
                        const to = window.prompt(`Email ${st.number} to:`, st.customer_email || "");
                        if (to) run("POST", `/statements/${st.id}/email`, { to });
                      }}>{st.emailed_at ? "Email again" : "Email"}</button>
                    <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                      onClick={() => window.confirm(`Cancel ${st.number}? Its calls and numbers are unlocked so they can be billed again. The number stays on record.`)
                        && run("POST", `/statements/${st.id}/cancel`)}>Cancel</button>
                  </>
                )}
              </td>
            </tr>
          ))}
          {!list.length && <tr><td colSpan={6} className="panel-empty">No statements for {month}.</td></tr>}
        </tbody>
      </table>
    </section>
  );
}
