import React, { useCallback, useEffect, useState } from "react";
import { Greeting } from "./IvrSection";

// Call recordings: see backend/recordings.py. Which extensions record every
// call is set under Extensions; a call in progress can be recorded from
// Status (Record).

const DIRECTIONS = { in: "Incoming", out: "Outgoing", int: "Internal", req: "Recorded on request" };

function when(started) {
  // 20261029-101500, in the PBX's time zone
  return `${started.slice(6, 8)}/${started.slice(4, 6)}/${started.slice(0, 4)} ${started.slice(9, 11)}:${started.slice(11, 13)}`;
}

function length(s) {
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export default function RecordingsSection({ config, send, busy, apiBase }) {
  const [data, setData] = useState(null);
  const [ext, setExt] = useState("");
  const [q, setQ] = useState("");
  const [days, setDays] = useState("");
  const [playing, setPlaying] = useState(null); // name

  const load = useCallback(async () => {
    const res = await fetch(`${apiBase}/api/pbx/recordings?ext=${encodeURIComponent(ext)}&q=${encodeURIComponent(q)}`);
    if (res.ok) {
      const d = await res.json();
      setData(d);
      setDays((v) => v || String(d.days));
    }
  }, [apiBase, ext, q]);
  useEffect(() => { load(); }, [load, config]);

  if (!data) return <section className="panel"><p className="panel-empty">Loading…</p></section>;
  const recording = config.extensions.filter((e) => e.record);
  const fileUrl = (name, download) => `${apiBase}/api/pbx/recordings/file/${encodeURIComponent(name)}${download ? "?download=1" : ""}`;

  return (
    <>
      <section className="panel">
        <h2>Call recording</h2>
        <p className="pbx-hint pbx-intro">
          Turn on &ldquo;Record every call&rdquo; for an extension under Extensions, or press Record next to a call in
          progress on the Status page. People must be told calls are recorded: with any extension recording every call,
          outside callers hear the notice below before they&rsquo;re put through, and people you call hear it when they
          answer, before you&rsquo;re connected. A call recorded with the Record button isn&rsquo;t announced: tell the other
          person yourself.
        </p>
        {!data.ready && (
          <div className="pbx-warning">The dashboard can&rsquo;t use {data.dir}. Create it and allow it in the service
            (README, &ldquo;Call recording&rdquo;); until then nothing is recorded.</div>
        )}
        <p className="pbx-hint">
          Recording every call: {recording.length ? recording.map((e) => `${e.ext} ${e.name}`).join(", ") : "no extensions"}.
        </p>
        {recording.length > 0 && !data.notice && (
          <div className="pbx-warning">Extensions record every call but there&rsquo;s no notice yet: add one below.</div>
        )}
        <Greeting path="/recordings/notice" has={data.notice} what="notice" send={send} busy={busy} apiBase={apiBase}
          label={<>Notice: {data.notice ? "set" : <span className="pbx-hint">none yet</span>}
            <span className="pbx-hint"> e.g. &ldquo;Calls may be recorded for training and quality.&rdquo;</span></>} />
        <form className="idea-form-row pbx-gap" onSubmit={async (e) => {
          e.preventDefault();
          if (await send("PUT", "/recordings/settings", { days: Number(days) }, `Recordings are kept for ${days} days.`)) load();
        }}>
          <label className="idea-field">
            Keep recordings for
            <input className="idea-input pbx-num-input" type="number" min={1} max={3650} value={days}
              onChange={(e) => setDays(e.target.value)} />
            days
          </label>
          <button className="ticket-action-btn" disabled={busy || Number(days) === data.days}>Save</button>
          <span className="pbx-hint">Older ones are deleted automatically.</span>
        </form>
      </section>

      <section className="panel pbx-gap">
        <h2>Recordings</h2>
        <div className="idea-form-row pbx-add-row">
          <select className="idea-select pbx-select" value={ext} onChange={(e) => setExt(e.target.value)}>
            <option value="">All extensions</option>
            {config.extensions.map((e) => <option key={e.ext} value={e.ext}>{e.ext} · {e.name}</option>)}
          </select>
          <input className="idea-input" placeholder="Search a number" value={q} onChange={(e) => setQ(e.target.value)} />
          <button className="ticket-action-btn" onClick={load}>Refresh</button>
        </div>
        {data.recordings.length === 0 ? <p className="panel-empty">No recordings{ext || q ? " match" : " yet"}.</p> : (
          <table className="users-table">
            <thead><tr><th>When ({data.timezone})</th><th>Extension</th><th>Other party</th><th>Call</th><th>Length</th><th></th></tr></thead>
            <tbody>
              {data.recordings.flatMap((r) => {
                const rows = [
                  <tr key={r.name}>
                    <td>{when(r.started)}</td>
                    <td>{r.ext}</td>
                    <td>{r.other === "unknown" ? "Unknown" : r.other}</td>
                    <td>{DIRECTIONS[r.direction]}</td>
                    <td>{length(r.seconds)}</td>
                    <td className="pbx-actions">
                      <button className="ticket-action-btn" onClick={() => setPlaying(playing === r.name ? null : r.name)}>
                        {playing === r.name ? "Close" : "Play"}</button>
                      <a className="ticket-action-btn" href={fileUrl(r.name, true)}>Download</a>
                      <button className="ticket-action-btn idea-delete-btn" disabled={busy}
                        onClick={async () => window.confirm("Delete this recording? It can't be undone.") &&
                          (await send("DELETE", `/recordings/file/${encodeURIComponent(r.name)}`, null, "Recording deleted.")) && load()}
                      >Delete</button>
                    </td>
                  </tr>,
                ];
                if (playing === r.name) {
                  rows.push(
                    <tr key={`${r.name}-play`}>
                      <td colSpan={6}><audio controls autoPlay src={fileUrl(r.name, false)} className="pbx-audio" /></td>
                    </tr>
                  );
                }
                return rows;
              })}
            </tbody>
          </table>
        )}
        {data.total > data.recordings.length && (
          <p className="pbx-hint">Showing the newest {data.recordings.length} of {data.total}. Filter by extension or number to find older ones.</p>
        )}
      </section>
    </>
  );
}
