import React, { useCallback, useEffect, useState } from "react";
import { get } from "./api";
import { Field, Message } from "./ui";

// Billing, for admins: statements (PDF), this month so far, and calls.
// Every amount is what the organisation pays, in euro.

const thisMonth = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};

const monthName = (m) => {
  const [y, mo] = m.split("-").map(Number);
  return new Date(y, mo - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
};

const talk = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

export function Billing() {
  const [view, setView] = useState("month");
  return (
    <section>
      <h2>Billing</h2>
      <nav className="tabs" aria-label="Billing">
        {[["month", "This month"], ["statements", "Statements"], ["calls", "Calls"]].map(([k, label]) => (
          <button key={k} className={`tab ${view === k ? "tab-on" : ""}`} aria-current={view === k ? "page" : undefined}
            onClick={() => setView(k)}>{label}</button>
        ))}
      </nav>
      {view === "month" && <MonthSoFar />}
      {view === "statements" && <Statements />}
      {view === "calls" && <Calls />}
    </section>
  );
}

function MonthPicker({ month, setMonth }) {
  return (
    <Field label="Month">
      <input type="month" value={month} max={thisMonth()} onChange={(e) => setMonth(e.target.value || thisMonth())} />
    </Field>
  );
}

function useLoad(path) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback(() => {
    setData(null);
    get(path).then((r) => { setData(r); setError(null); }).catch((e) => setError(e.message));
  }, [path]);
  useEffect(() => { load(); }, [load]);
  return { data, error };
}

function MonthSoFar() {
  const [month, setMonth] = useState(thisMonth());
  const { data, error } = useLoad(`/billing/month?month=${month}`);
  return (
    <>
      <MonthPicker month={month} setMonth={setMonth} />
      {error && <Message kind="error">{error}</Message>}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && (
        <>
          <p className="small muted">
            {data.month_open ? `${monthName(data.month)} so far: an estimate of your next statement, updated every few minutes. `
              : `What ${monthName(data.month)} comes to, not yet on a statement. `}
            The statement is what you pay.
          </p>
          {data.customers.length === 0 && <p className="muted">Nothing to show: billing isn't set up for your account yet.</p>}
          {data.customers.map((c) => (
            <div className="card" key={c.customer}>
              {data.customers.length > 1 && <h3>{c.customer}</h3>}
              <table className="table">
                <tbody>
                  {c.numbers.map((n) => <tr key={n.number}><td>{n.label || "Phone number"} {n.number}</td><td className="num">€{n.amount}</td></tr>)}
                  {c.addons.map((a) => <tr key={a.label}><td>{a.label}</td><td className="num">€{a.amount}</td></tr>)}
                  <tr><td>Calls ({c.calls.count}, {talk(c.calls.seconds)})</td><td className="num">€{c.calls.amount}</td></tr>
                  <tr className="total"><td>Total so far</td><td className="num">€{c.total}</td></tr>
                </tbody>
              </table>
              {c.not_priced > 0 && <p className="small muted">{c.not_priced} call{c.not_priced === 1 ? " isn't" : "s aren't"} priced
                yet, so not in the total.</p>}
              {c.billed_already > 0 && <p className="small muted">{c.billed_already} call{c.billed_already === 1 ? " is" : "s are"} on
                a statement already, so not in this total.</p>}
              {c.by_destination.length > 0 && (
                <details>
                  <summary>Calls by destination</summary>
                  <table className="table small">
                    <thead><tr><th>Destination</th><th className="num">Calls</th><th className="num">Time</th><th className="num">Amount</th></tr></thead>
                    <tbody>
                      {c.by_destination.map((g) => (
                        <tr key={g.destination}><td>{g.destination}</td><td className="num">{g.calls}</td>
                          <td className="num">{talk(g.seconds)}</td><td className="num">€{g.amount}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}
            </div>
          ))}
        </>
      )}
    </>
  );
}

function Statements() {
  const { data, error } = useLoad("/billing/statements");
  if (error) return <Message kind="error">{error}</Message>;
  if (!data) return <p className="muted">Loading…</p>;
  if (!data.statements.length) return <p className="muted">No statements yet. Each month&rsquo;s comes after the month ends.</p>;
  const several = new Set(data.statements.map((s) => s.customer)).size > 1;
  return (
    <ul className="list">
      {data.statements.map((s) => (
        <li key={s.id} className="row-head static">
          <span className="grow">{monthName(s.month)}<span className="small muted block">{s.number}
            {several ? ` · ${s.customer}` : ""} · issued {new Date(s.issued_at).toLocaleDateString()}</span></span>
          <span className="ext">€{s.total}</span>
          <a className="btn btn-secondary" href={`/api/billing/statements/${s.id}/pdf`} target="_blank" rel="noreferrer">PDF</a>
        </li>
      ))}
    </ul>
  );
}

function Calls() {
  const [month, setMonth] = useState(thisMonth());
  const [ext, setExt] = useState("");
  const [exts, setExts] = useState([]);
  useEffect(() => { get("/extensions").then((r) => setExts(r.extensions)).catch(() => setExts([])); }, []);
  const { data, error } = useLoad(`/billing/calls?month=${month}&ext=${encodeURIComponent(ext)}`);
  const state = { billed: "", estimate: "estimate", "not priced": "not priced yet" };
  return (
    <>
      <div className="inline-form">
        <MonthPicker month={month} setMonth={setMonth} />
        <Field label="Extension">
          <select value={ext} onChange={(e) => setExt(e.target.value)}>
            <option value="">All</option>
            {exts.map((e) => <option key={e.ext} value={e.ext}>{e.ext} {e.name}</option>)}
          </select>
        </Field>
      </div>
      {error && <Message kind="error">{error}</Message>}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && (data.calls.length === 0 ? <p className="muted">No calls out in {monthName(data.month)}.</p> : (
        <>
          <p className="small muted">Calls out that connected, newest first. Amounts marked &ldquo;estimate&rdquo; aren&rsquo;t on a statement yet.</p>
          <div className="table-wrap">
            <table className="table small">
              <thead><tr><th>When</th><th>Ext</th><th>Number</th><th>Destination</th><th className="num">Time</th><th className="num">Amount</th></tr></thead>
              <tbody>
                {data.calls.map((c, i) => (
                  <tr key={i}>
                    <td>{c.start.slice(5, 16)}</td><td>{c.ext}</td><td>{c.number}</td><td>{c.destination}</td>
                    <td className="num">{talk(c.seconds)}</td>
                    <td className="num">{c.amount == null ? <span className="muted">—</span> : `€${c.amount}`}
                      {state[c.state] && <span className="small muted block">{state[c.state]}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.truncated && <p className="small muted">Showing the newest {data.calls.length}: choose an extension to see more.</p>}
        </>
      ))}
    </>
  );
}
