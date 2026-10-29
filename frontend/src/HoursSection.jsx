import React, { useState } from "react";
import { TargetSelect, describeTarget } from "./IvrSection";

// Business hours: see backend/hours.py. A schedule sends calls to one place
// when open and another when closed; point an inbound number, a call link
// or an IVR key at it.

const DAYS = [["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"], ["fri", "Friday"],
  ["sat", "Saturday"], ["sun", "Sunday"]];
const WEEKDAYS = { mon: [["09:00", "17:30"]], tue: [["09:00", "17:30"]], wed: [["09:00", "17:30"]],
  thu: [["09:00", "17:30"]], fri: [["09:00", "17:30"]], sat: [], sun: [] };
const MAX_RANGES = 4;

function weekSummary(week) {
  const text = (d) => week[d].map(([a, b]) => `${a}-${b}`).join(", ");
  // Run together days with the same hours: "Mon-Fri 09:00-17:30".
  const parts = [];
  DAYS.forEach(([d]) => {
    const last = parts[parts.length - 1];
    if (last && text(last.to) === text(d)) last.to = d;
    else parts.push({ from: d, to: d });
  });
  const cap = (d) => d[0].toUpperCase() + d.slice(1);
  return parts.filter((p) => week[p.from].length)
    .map((p) => `${cap(p.from)}${p.from === p.to ? "" : `-${cap(p.to)}`} ${text(p.from)}`).join(" · ") || "Never open";
}

// Splits the day's last opening around lunch (09:00-17:30 becomes
// 09:00-13:00 and 14:00-17:30), or adds an hour after it.
function addBreak(ranges) {
  const [a, b] = ranges[ranges.length - 1];
  if (a < "13:00" && b > "14:00") return [...ranges.slice(0, -1), [a, "13:00"], ["14:00", b]];
  const h = Number(b.slice(0, 2));
  return h < 23 ? [...ranges, [`${String(h + 1).padStart(2, "0")}${b.slice(2)}`, "23:59"]] : ranges;
}

function WeekEditor({ week, onChange }) {
  const setDay = (d, ranges) => onChange({ ...week, [d]: ranges });
  return (
    <div className="pbx-week">
      {DAYS.map(([d, name]) => (
        <div key={d} className="pbx-week-row">
          <label className="pbx-toggle pbx-week-day">
            <input type="checkbox" checked={week[d].length > 0}
              onChange={(e) => setDay(d, e.target.checked ? [["09:00", "17:30"]] : [])} />
            {name}
          </label>
          {week[d].length === 0 ? <span className="pbx-hint">Closed</span> : (
            <div className="pbx-week-ranges">
              {week[d].map(([a, b], i) => (
                <span key={i} className="pbx-week-range">
                  <input type="time" className="idea-input" value={a} required
                    onChange={(e) => setDay(d, week[d].map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
                  –
                  <input type="time" className="idea-input" value={b} required
                    onChange={(e) => setDay(d, week[d].map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
                  {week[d].length > 1 && (
                    <button type="button" className="ticket-action-btn" title="Remove"
                      onClick={() => setDay(d, week[d].filter((_, j) => j !== i))}>×</button>
                  )}
                </span>
              ))}
              {week[d].length < MAX_RANGES && (
                <button type="button" className="ticket-action-btn"
                  onClick={() => setDay(d, addBreak(week[d]))}>Add a break</button>
              )}
            </div>
          )}
          {d === "mon" && week.mon.length > 0 && (
            <button type="button" className="ticket-action-btn pbx-push"
              onClick={() => onChange({ ...week, tue: week.mon, wed: week.mon, thu: week.mon, fri: week.mon })}>
              Same Tue-Fri</button>
          )}
        </div>
      ))}
    </div>
  );
}

const EMPTY = { number: "", name: "", week: WEEKDAYS, holidays: [], open_target: "", closed_target: "", override: "" };

export default function HoursSection({ config, send, busy }) {
  const [form, setForm] = useState(null); // null | { ...fields, isNew }
  const [day, setDay] = useState("");

  const save = async () => {
    const body = { name: form.name, week: form.week, holidays: form.holidays, open_target: form.open_target,
      closed_target: form.closed_target, override: form.override };
    const ok = form.isNew
      ? await send("POST", "/hours", { ...body, number: form.number.trim() }, "Business hours created. Point an inbound number or call link at it.")
      : await send("PUT", `/hours/${form.number}`, body, "Business hours saved.");
    if (ok) setForm(null);
  };
  const override = (h, value, text) => send("PUT", `/hours/${h.number}/override`, { override: value }, text);

  return (
    <section className="panel">
      <h2>Business hours</h2>
      <p className="pbx-hint pbx-intro">
        Send calls to one place when you&rsquo;re open and another when you&rsquo;re closed: for example the main menu
        during the day and voicemail at night. Point an inbound number, a call link or an IVR key at the schedule.
        Times are {config.timezone} time. Days you add as closed (bank holidays) are closed all day.
      </p>

      {config.business_hours.length === 0 && !form && <p className="panel-empty">No schedules yet.</p>}
      <ul className="idea-list">
        {config.business_hours.map((h) => (
          <li key={h.number} className="idea-item">
            <div className="idea-item-head">
              <span className="idea-priority idea-priority-medium">{h.number}</span>
              <span className="idea-title">{h.name}</span>
              <span className={`pbx-chip ${h.open_now ? "" : "pbx-chip-off"}`}>
                {h.open_now ? "Open now" : "Closed now"}{h.override ? " (set by hand)" : ""}
              </span>
            </div>
            <div className="pbx-hint">{weekSummary(h.week)}</div>
            {h.holidays.length > 0 && <div className="pbx-hint">Closed on {h.holidays.join(", ")}</div>}
            <div className="pbx-hint">
              Open: {describeTarget(config, h.open_target)} · Closed: {describeTarget(config, h.closed_target)}
            </div>
            <div className="idea-item-foot">
              {h.override !== "closed" && (
                <button className="ticket-action-btn" disabled={busy}
                  onClick={() => override(h, "closed", "Closed until you switch it back.")}>Close now</button>
              )}
              {h.override !== "open" && (
                <button className="ticket-action-btn" disabled={busy}
                  onClick={() => override(h, "open", "Open until you switch it back.")}>Open now</button>
              )}
              {h.override && (
                <button className="ticket-action-btn" disabled={busy}
                  onClick={() => override(h, "", "Back to the hours.")}>Back to the hours</button>
              )}
              <button className="ticket-action-btn" onClick={() => setForm({ ...h, isNew: false })}>Edit</button>
              <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                onClick={() => window.confirm(`Delete business hours ${h.number} (${h.name})?`) &&
                  send("DELETE", `/hours/${h.number}`, null, "Business hours deleted.")}
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
                title={`Leave blank for the next free number (${config.limits.hours_min}-${config.limits.hours_max})`}
                onChange={(e) => setForm({ ...form, number: e.target.value })} />
            )}
            <input className="idea-input pbx-name-input" placeholder="Name, e.g. Office hours" maxLength={40}
              value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>

          <div className="ticket-action-label pbx-gap">Open</div>
          <WeekEditor week={form.week} onChange={(week) => setForm({ ...form, week })} />

          <div className="ticket-action-label pbx-gap">Also closed on</div>
          <div className="pbx-chips">
            {form.holidays.map((d) => (
              <span key={d} className="pbx-chip">
                {d}{" "}
                <button type="button" className="save-toast-close" aria-label={`Remove ${d}`}
                  onClick={() => setForm({ ...form, holidays: form.holidays.filter((x) => x !== d) })}>×</button>
              </span>
            ))}
          </div>
          <div className="idea-form-row">
            <input type="date" className="idea-input" value={day} onChange={(e) => setDay(e.target.value)} />
            <button type="button" className="ticket-action-btn" disabled={!day || form.holidays.includes(day)}
              onClick={() => { setForm({ ...form, holidays: [...form.holidays, day].sort() }); setDay(""); }}>Add day</button>
          </div>

          <label className="pbx-field pbx-gap">
            <span>When open, calls go to</span>
            <TargetSelect config={config} self="" schedules={false} value={form.open_target} empty="Choose…"
              onChange={(v) => setForm({ ...form, open_target: v })} />
          </label>
          <label className="pbx-field">
            <span>When closed, calls go to</span>
            <TargetSelect config={config} self="" schedules={false} value={form.closed_target} empty="Choose…"
              onChange={(v) => setForm({ ...form, closed_target: v })} />
          </label>

          <div className="ticket-action-buttons">
            <button className="ticket-action-btn" onClick={() => setForm(null)}>Cancel</button>
            <button className="ticket-action-btn ticket-action-btn-primary"
              disabled={busy || !form.name.trim() || !form.open_target || !form.closed_target} onClick={save}>
              {form.isNew ? "Create" : "Save"}
            </button>
          </div>
          <p className="pbx-hint">Past closed days are removed when you save.</p>
        </div>
      ) : (
        <button className="ticket-action-btn ticket-action-btn-primary pbx-gap" onClick={() => setForm({ ...EMPTY, isNew: true })}>
          New schedule
        </button>
      )}
    </section>
  );
}
