import React, { useEffect } from "react";

// A small panel fixed to the bottom-right of the screen, so a save shows
// that it's running, and then how it went, wherever the page is scrolled
// (the result line at the top of a page is out of sight from a Save
// button further down).
//   toast: null | { busy: true, text } | { ok: boolean, text }
export default function SaveToast({ toast, onClose }) {
  useEffect(() => {
    if (!toast || toast.busy || !toast.ok) return undefined; // errors stay until closed
    const t = setTimeout(onClose, 5000);
    return () => clearTimeout(t);
  }, [toast, onClose]);

  if (!toast) return null;
  return (
    <div className={`save-toast ${toast.busy ? "" : toast.ok ? "save-toast-ok" : "save-toast-error"}`} role="status" aria-live="polite">
      <div className="save-toast-text">
        {toast.text}
        {!toast.busy && <button className="save-toast-close" onClick={onClose} aria-label="Close">×</button>}
      </div>
      {toast.busy && (
        <div className="pbx-progress"><div className="pbx-progress-bar pbx-progress-busy" /></div>
      )}
    </div>
  );
}

// What to say while a request is running.
export function busyText(method, path, labels = []) {
  const hit = labels.find(([re]) => re.test(path));
  if (hit) return hit[1];
  return method === "DELETE" ? "Deleting…" : "Saving…";
}
