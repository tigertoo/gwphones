import React, { useState } from "react";
import PbxPanel, { numberFromTel } from "./PbxPanel";
import "./gwphones.css";

// The operator console: the Phone System (extensions, numbers, menus,
// tenants, billing...), all of it in PbxPanel. Served by nginx behind its
// login; the API is under API_BASE (see vite.config.js and the README).
const API_BASE = import.meta.env.VITE_GWPHONES_API || "/phone-api";

export default function App() {
  // ?call=tel:... (this page as the browser's tel: link handler) opens
  // Click to Call with the number filled in, never calling by itself.
  const [callNumber] = useState(() => {
    const raw = new URLSearchParams(window.location.search).get("call");
    if (!raw) return "";
    window.history.replaceState(null, "", window.location.pathname);
    return numberFromTel(raw);
  });

  return (
    <div className="admin-dash">
      <header className="admin-dash-header">
        <div className="admin-dash-title">
          <h1>gwphones</h1>
        </div>
      </header>
      <PbxPanel apiBase={API_BASE} callNumber={callNumber} />
    </div>
  );
}
