import React, { useCallback, useEffect, useState } from "react";
import { CustomersAndNumbers, MonthlySummary, Statements } from "./BillingCustomers";

// Phone System → Billing: calls read from Asterisk's CDR log, priced from
// the uploaded rate sheets (backend/billing.py, /api/billing/*), and what
// each customer owes for them (BillingCustomers.jsx).

const STATUS_LABELS = {
  rated: "Priced",
  provisional: "Provisional",
  unrated: "Not priced",
  free: "Free",
};
const DIRECTIONS = { out: "Outbound", in: "Inbound", internal: "Internal" };

function thisMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export default function BillingPanel({ apiBase }) {
  const [month, setMonth] = useState(thisMonth());
  const [overview, setOverview] = useState(null);
  const [calls, setCalls] = useState([]);
  const [filter, setFilter] = useState({ direction: "out", status: "" });
  const [open, setOpen] = useState(null); // call id with its pricing shown
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0); // bumped after every change, so every part reloads

  const api = useCallback(async (method, path, body) => {
    const res = await fetch(`${apiBase}/api/billing${path}`, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
    return data;
  }, [apiBase]);

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams({ month, ...filter });
      const [o, c] = await Promise.all([api("GET", `/overview?month=${month}`), api("GET", `/calls?${q}`)]);
      setOverview(o);
      setCalls(c.calls);
    } catch (e) {
      setResult({ ok: false, text: e.message });
    }
  }, [api, month, filter]);

  useEffect(() => { load(); }, [load]);

  const run = async (method, path, body) => {
    setBusy(true);
    setResult(null);
    try {
      const data = await api(method, path, body);
      setResult({ ok: true, text: (data.messages || ["Done."]).join(" ") });
      await load();
      setVersion((v) => v + 1);
      return data;
    } catch (e) {
      setResult({ ok: false, text: e.message });
      return null;
    } finally {
      setBusy(false);
    }
  };

  if (!overview) return <section className="panel"><p className="panel-empty">Loading…</p></section>;
  const csv = `${apiBase}/api/billing/calls.csv?${new URLSearchParams({ month, ...filter })}`;
  const needs = (overview.counts.unrated || 0) + (overview.counts.provisional || 0);

  return (
    <>
      {result && (
        <div className={`ticket-action-result ticket-action-result-${result.ok ? "success" : "error"} pbx-result`}>{result.text}</div>
      )}

      <section className="panel">
        <h2>Calls and costs</h2>
        <div className="idea-form-row">
          <label className="pbx-field-inline">
            Month{" "}
            <input className="idea-input pbx-month-input" type="month" value={month} onChange={(e) => setMonth(e.target.value || thisMonth())} />
          </label>
          <button className="ticket-action-btn" disabled={busy} onClick={() => run("POST", "/refresh")}>Read new calls now</button>
          <button className="ticket-action-btn" disabled={busy} onClick={() => run("POST", `/reprice?month=${month}`)}>Re-price this month</button>
          <a className="ticket-action-btn" href={csv}>Download CSV</a>
        </div>
        <div className="pbx-trunk-grid pbx-gap">
          <div className="vital-card vital-ok">
            <div className="vital-card-top"><span className="vital-label">Outbound calls</span></div>
            <div className="vital-number">{overview.outbound}</div>
            <div className="vital-detail">{overview.outbound_minutes} minutes</div>
          </div>
          <div className="vital-card vital-ok">
            <div className="vital-card-top"><span className="vital-label">Cost to you</span></div>
            <div className="vital-number">€{overview.cost_eur}</div>
            <div className="vital-detail">priced calls only</div>
          </div>
          <div className={`vital-card vital-${needs ? "warning" : "ok"}`}>
            <div className="vital-card-top"><span className="vital-label">Not priced yet</span></div>
            <div className="vital-number">{needs}</div>
            <div className="vital-detail">
              {overview.counts.unrated || 0} not priced · {overview.counts.provisional || 0} provisional
            </div>
          </div>
        </div>
        <p className="pbx-hint">
          {overview.cdr_found
            ? `Calls are read from ${overview.cdr_file} every minute${overview.cdr_read_at ? `, last at ${new Date(overview.cdr_read_at).toLocaleTimeString()}` : ""}.`
            : `Asterisk's call log ${overview.cdr_file} wasn't found.`}
          {" "}Costs are in euro at the ECB rate for the day of each call.
        </p>
      </section>

      <MonthlySummary apiBase={apiBase} api={api} month={month} version={version} />
      <Statements apiBase={apiBase} api={api} run={run} busy={busy} month={month} version={version} />
      <CustomersAndNumbers api={api} run={run} busy={busy} version={version} />
      <RateSheets overview={overview} run={run} api={api} busy={busy} setResult={setResult} />

      <section className="panel pbx-gap">
        <h2>Calls</h2>
        <div className="idea-form-row">
          <select className="idea-select pbx-select" value={filter.direction}
            onChange={(e) => setFilter({ ...filter, direction: e.target.value })}>
            <option value="">All directions</option>
            {Object.entries(DIRECTIONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <select className="idea-select pbx-select" value={filter.status}
            onChange={(e) => setFilter({ ...filter, status: e.target.value })}>
            <option value="">All</option>
            {Object.entries(STATUS_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <span className="pbx-hint">{calls.length} call{calls.length === 1 ? "" : "s"}. Click one to see how it was priced.</span>
        </div>
        {calls.length === 0 ? <p className="panel-empty">No calls.</p> : (
          <table className="users-table">
            <thead>
              <tr><th>When</th><th>Ext</th><th>Number</th><th>Via</th><th>Talk</th><th>Destination</th><th>Cost €</th><th>Status</th></tr>
            </thead>
            <tbody>
              {calls.flatMap((c) => {
                const rows = [
                  <tr key={c.id} className="pbx-call-row" onClick={() => setOpen(open === c.id ? null : c.id)}>
                    <td>{c.start.slice(5, 16)}</td>
                    <td>{c.ext || "—"}</td>
                    <td>{c.direction === "in" ? `${c.caller} → +${c.number}` : c.direction === "out" && c.number ? `+${c.number}` : c.number || c.dialled}</td>
                    <td>{c.trunk || DIRECTIONS[c.direction]}</td>
                    <td>{Math.floor(c.billsec / 60)}:{String(c.billsec % 60).padStart(2, "0")}</td>
                    <td>{c.destination || "—"}</td>
                    <td>{c.cost_eur != null ? Number(c.cost_eur).toFixed(4) : "—"}</td>
                    <td><span className={`pbx-bill-status pbx-bill-${c.status}`} title={c.reason || ""}>{STATUS_LABELS[c.status]}</span></td>
                  </tr>,
                ];
                if (open === c.id) rows.push(
                  <tr key={`${c.id}-trace`}>
                    <td colSpan={8} className="ticket-action-panel">
                      <PricingTrace call={c} sheet={overview.sheets.find((s) => s.id === c.sheet_id)} />
                    </td>
                  </tr>
                );
                return rows;
              })}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

function PricingTrace({ call: c, sheet }) {
  const lines = [
    ["Number sent", c.number ? `+${c.number}` : c.dialled],
    ["Talk time", `${c.billsec} s (${c.disposition?.toLowerCase()})`],
  ];
  if (c.prefix) {
    lines.push(
      ["Matched", `${c.prefix} · ${c.destination || "(no name)"} · sheet #${c.sheet_id}`],
      ["Rate", `${c.rate} ${c.currency}/min ex VAT${sheet && (sheet.rate_unit === "cents" || Number(sheet.vat_pct))
        ? ` (the sheet gives ${sheet.rate_unit === "cents" ? "cents" : c.currency}${Number(sheet.vat_pct) ? `, incl. ${sheet.vat_pct}% VAT` : ""})` : ""}`],
      ["Charged for", `${c.bill_seconds} s (billing increments)`],
      ["Connection fee", `${c.conn_fee} ${c.currency}`],
      ["Cost", `${c.cost} ${c.currency}`],
    );
    if (c.fx_rate) lines.push(["Exchange rate", c.currency === "EUR" ? "—" : `1 ${c.currency} = ${c.fx_rate} EUR (ECB, ${c.fx_date})`]);
    if (c.cost_eur != null) lines.push(["Cost in euro", `€${c.cost_eur}`]);
  }
  if (c.reason) lines.push(["Note", c.reason]);
  return (
    <table className="pbx-trace">
      <tbody>{lines.map(([k, v]) => <tr key={k}><td className="pbx-hint">{k}</td><td>{v}</td></tr>)}</tbody>
    </table>
  );
}

function RateSheets({ overview, run, api, busy, setResult }) {
  const [upload, setUpload] = useState(null); // { route, file, b64, preview, mapping, defaults }

  const choose = async (routeKey, file) => {
    if (!file) return;
    const [trunk, route] = routeKey.split("|");
    try {
      const b64 = await readAsBase64(file);
      const preview = await api("POST", "/rate-sheets/preview", { trunk, route, filename: file.name, content_b64: b64 });
      setUpload({ trunk, route, file, b64, preview, mapping: preview.mapping, defaults: preview.defaults });
    } catch (e) {
      setResult({ ok: false, text: e.message });
    }
  };

  const remap = async (mapping, defaults = upload.defaults) => {
    try {
      const preview = await api("POST", "/rate-sheets/preview", {
        trunk: upload.trunk, route: upload.route, filename: upload.file.name, content_b64: upload.b64, mapping, defaults,
      });
      setUpload({ ...upload, preview, mapping, defaults });
    } catch (e) {
      setResult({ ok: false, text: e.message });
    }
  };

  const doImport = async () => {
    const data = await run("POST", "/rate-sheets", {
      trunk: upload.trunk, route: upload.route, filename: upload.file.name, content_b64: upload.b64,
      mapping: upload.mapping, defaults: upload.defaults,
    });
    if (data) setUpload(null);
  };

  const label = (t, r) => overview.routes.find((x) => x.trunk === t && x.route === r)?.label || t;

  return (
    <section className="panel pbx-gap">
      <h2>Rate sheets</h2>
      <p className="pbx-hint pbx-intro">
        Upload each provider&rsquo;s own spreadsheet (CSV, tab-separated or Excel), one per Voxbeam route. Every
        upload is kept: each call is priced from the sheet in force when it was made.
      </p>
      <table className="users-table">
        <thead><tr><th>Route</th><th>Current sheet</th><th>Rates</th><th>Valid</th><th>Upload a new one</th></tr></thead>
        <tbody>
          {overview.routes.map((r) => {
            const sheets = overview.sheets.filter((s) => s.trunk === r.trunk && s.route === r.route);
            const s = sheets[0];
            return (
              <tr key={`${r.trunk}|${r.route}`}>
                <td>{r.label}</td>
                <td>{s ? <>{s.filename} <span className="pbx-hint">({new Date(s.uploaded_at).toLocaleDateString()}{sheets.length > 1 ? `, ${sheets.length - 1} older` : ""})</span></> : <span className="pbx-hint">None yet</span>}</td>
                <td>{s ? s.rows.toLocaleString() : "—"}</td>
                <td>
                  {s ? <>{s.valid_from?.slice(0, 10)} → {s.valid_to ? s.valid_to.slice(0, 10) : "no end"}
                    {s.expired && <span className="pbx-bill-status pbx-bill-provisional"> expired</span>}</> : "—"}
                </td>
                <td>
                  <input type="file" accept=".csv,.tsv,.txt,.xlsx" disabled={busy}
                    onChange={(e) => { choose(`${r.trunk}|${r.route}`, e.target.files[0]); e.target.value = ""; }} />
                  {s && (
                    <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                      onClick={() => window.confirm(`Delete ${s.filename}? Calls priced from it will be re-priced from the previous sheet, or show as not priced.`)
                        && run("DELETE", `/rate-sheets/${s.id}`)}>Delete</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {upload && (
        <div className="ticket-action-panel pbx-gap">
          <div className="ticket-action-label">
            {upload.file.name} for {label(upload.trunk, upload.route)}: check which column is which
          </div>
          <div className="pbx-mapping">
            {Object.entries(upload.preview.fields).map(([field, f]) => (
              <label key={field} className="pbx-field">
                <span>{f.label}{f.required ? " *" : ""}</span>
                <select className="idea-select pbx-select" value={upload.mapping[field] ?? ""}
                  onChange={(e) => remap({ ...upload.mapping, [field]: e.target.value === "" ? null : Number(e.target.value) })}>
                  <option value="">— none —</option>
                  {upload.preview.columns.map((c, i) => <option key={i} value={i}>{c}</option>)}
                </select>
              </label>
            ))}
          </div>
          <div className="pbx-mapping">
            <label className="pbx-field">
              <span>Prices in the sheet are</span>
              <select className="idea-select pbx-select" value={upload.defaults.rate_unit}
                onChange={(e) => remap(upload.mapping, { ...upload.defaults, rate_unit: e.target.value })}>
                <option value="units">per minute in euro/dollars (e.g. 0.045)</option>
                <option value="cents">per minute in cents (e.g. 4.5)</option>
              </select>
            </label>
            <label className="pbx-field">
              <span>VAT included in the prices, %</span>
              <input className="idea-input" value={upload.defaults.vat_pct}
                onChange={(e) => setUpload({ ...upload, defaults: { ...upload.defaults, vat_pct: e.target.value } })}
                onBlur={() => remap(upload.mapping, upload.defaults)} />
            </label>
            {[["currency", "Currency if no column"], ["conn_unit", "First increment (s) if no column"],
              ["bill_unit", "Next increments (s) if no column"], ["conn_fee", "Connection fee if no column"]].map(([k, l]) => (
              <label key={k} className="pbx-field">
                <span>{l}</span>
                <input className="idea-input" value={upload.defaults[k]}
                  onChange={(e) => setUpload({ ...upload, defaults: { ...upload.defaults, [k]: e.target.value } })}
                  onBlur={() => remap(upload.mapping, {
                    ...upload.defaults, conn_unit: Number(upload.defaults.conn_unit) || 1, bill_unit: Number(upload.defaults.bill_unit) || 1,
                  })} />
              </label>
            ))}
          </div>

          <table className="users-table pbx-gap">
            <thead><tr>{upload.preview.columns.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
            <tbody>{upload.preview.sample.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody>
          </table>

          <SheetStats stats={upload.preview.stats} />

          <div className="ticket-action-buttons">
            <button className="ticket-action-btn" onClick={() => setUpload(null)}>Cancel</button>
            <button className="ticket-action-btn ticket-action-btn-primary"
              disabled={busy || upload.preview.stats.problem || !upload.preview.stats.imported} onClick={doImport}>
              Import {upload.preview.stats.imported ? upload.preview.stats.imported.toLocaleString() : ""} rates
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function SheetStats({ stats }) {
  if (stats.problem) return <div className="ticket-action-result ticket-action-result-error">{stats.problem}</div>;
  const list = (o) => Object.entries(o).map(([k, v]) => `${k} (${v.toLocaleString()})`).join(", ");
  return (
    <div className="pbx-hint pbx-gap">
      {stats.example && (
        <div className="pbx-sheet-example">
          Check: {stats.example.prefix} {stats.example.destination} is {stats.example.published} in the sheet, which will cost{" "}
          <b>{stats.example.net} {stats.example.currency} per minute</b>{" "}(ex VAT).
        </div>
      )}
      <div>{stats.rows.toLocaleString()} lines: {stats.imported.toLocaleString()} rates to import, {stats.skipped.toLocaleString()} skipped.</div>
      <div>Status column: {list(stats.statuses)}. Rows marked deleted or removed are skipped.</div>
      <div>Currencies: {list(stats.currencies) || "—"} · Increments: {list(stats.increments) || "—"}</div>
      {stats.change_dates.length > 0 && <div>Rate changes: {stats.change_dates[0].slice(0, 16)} → {stats.change_dates[1].slice(0, 16)} UTC</div>}
      {stats.expiry_dates.length > 0 && <div>Expiry: {stats.expiry_dates[0].slice(0, 16)} → {stats.expiry_dates[1].slice(0, 16)} UTC</div>}
      {stats.errors.length > 0 && <div>Couldn&rsquo;t read: {stats.errors.join("; ")}</div>}
    </div>
  );
}
