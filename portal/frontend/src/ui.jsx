import React, { useState } from "react";

// Small shared pieces: a form row, a button that shows it's working, and
// a message line.

export function Field({ label, hint, children }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Message({ kind = "info", children }) {
  if (!children) return null;
  return <div className={`message message-${kind}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
}

// Runs an async action, showing what happened: its error, or the
// success text (a function of the result, or a string).
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const run = async (fn, success) => {
    setBusy(true);
    setMsg(null);
    try {
      const result = await fn();
      const text = typeof success === "function" ? success(result) : success;
      if (result && result.pending) setMsg({ kind: "info", text: result.message });
      else if (text) setMsg({ kind: "ok", text });
      return result;
    } catch (e) {
      setMsg({ kind: "error", text: e.message });
      return undefined;
    } finally {
      setBusy(false);
    }
  };
  return { busy, msg, setMsg, run, show: msg ? <Message kind={msg.kind}>{msg.text}</Message> : null };
}

export function Button({ busy, kind = "primary", children, ...props }) {
  return (
    <button className={`btn btn-${kind}`} disabled={busy || props.disabled} {...props}>
      {busy ? "Working…" : children}
    </button>
  );
}
