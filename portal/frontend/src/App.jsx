import React, { useCallback, useEffect, useState } from "react";
import { get, post } from "./api";
import { Account } from "./Account";
import { Billing } from "./Billing";
import { Contacts } from "./Contacts";
import { DeskPhones } from "./DeskPhones";
import { Extensions, Recordings, SitesPage, Status } from "./Phones";
import { Routing } from "./Routing";
import { CodeStep, LinkPage, SetUpCodes, SignIn } from "./SignIn";
import { MicrosoftPage, Staff } from "./Staff";
import { Message } from "./ui";
import { takeMicrosoftResult } from "./microsoft";

// The portal: what's shown follows the session's state from the API
// (none, pending_mfa, enrol, full). Links from emails open /invite#... or
// /reset#...

// The menu, by role: headings with their pages. Account and Sign out are
// under the person's name at the top right.
const MENU = {
  admin: [
    ["Phones", [["extensions", "Extensions"], ["desk", "Desk phones"], ["status", "Status"]]],
    ["Calls", [["routing", "Call routing"], ["recordings", "Recordings"]]],
    ["Organisation", [["staff", "Staff"], ["contacts", "Contacts"], ["sites", "Sites"], ["microsoft", "Microsoft 365"]]],
    ["Billing", [["billing", "Statements and calls"]]],
  ],
  user: [[null, [["extensions", "My phone"], ["status", "Status"], ["recordings", "Recordings"]]]],
};


export default function App() {
  const path = window.location.pathname;
  if (path === "/invite") return <Frame><LinkPage kind="invite" /></Frame>;
  if (path === "/reset") return <Frame><LinkPage kind="reset" /></Frame>;
  return <Portal />;
}

function Frame({ tenant, children, right, wide }) {
  return (
    <div className={`app ${wide ? "app-wide" : ""}`}>
      <header className="top">
        <span className="brand">{tenant ? tenant.name : "Phone system"}</span>
        {right}
      </header>
      <main>{children}</main>
    </div>
  );
}

function Portal() {
  const [session, setSession] = useState(null);
  const [tenant, setTenant] = useState(null);
  const [tab, setTab] = useState("extensions");
  const [menuOpen, setMenuOpen] = useState(false);
  // Back from Microsoft (/?microsoft=...): its result, shown once; a link starts on Staff.
  const [notice] = useState(() => takeMicrosoftResult());
  useEffect(() => { if (notice?.tab) setTab(notice.tab); }, [notice]);
  const refresh = useCallback(() => get("/session").then(setSession).catch(() => setSession({ state: "none" })), []);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (session?.state === "full") get("/tenant").then(setTenant).catch(() => setTenant(null));
  }, [session?.state]);

  // Whoever signs in next starts on their main page, not where the last
  // person was (a shared device).
  const onState = (r) => {
    if (r.state !== "full") setTab("extensions");
    return r.state === "full" ? refresh() : setSession(r);
  };
  if (!session) return <Frame><p className="muted">Loading…</p></Frame>;
  if (session.state === "none") return <Frame><SignIn onState={onState} notice={notice} /></Frame>;
  if (session.state === "pending_mfa") return <Frame><CodeStep onState={onState} /></Frame>;
  if (session.state === "enrol") {
    return <Frame><SetUpCodes onDone={refresh}
      intro="As an admin, you need two-factor codes: your password alone can't sign you in." /></Frame>;
  }

  const me = session.me;
  const menu = MENU[me.role] || MENU.user;
  const go = (key) => { setTab(key); setMenuOpen(false); window.scrollTo(0, 0); };
  const current = menu.flatMap(([, items]) => items).find(([k]) => k === tab)?.[1] || "Account";
  const signOut = async () => {
    await post("/logout").catch(() => null);
    setSession({ state: "none" });
    setTenant(null);
    setTab("extensions");
  };
  return (
    <Frame tenant={tenant} wide right={
      <details className="usermenu">
        <summary>{me.name || me.email}</summary>
        <div className="usermenu-items">
          <button onClick={(e) => { e.currentTarget.closest("details").open = false; go("account"); }}>Account</button>
          <button onClick={signOut}>Sign out</button>
        </div>
      </details>}>
      <div className="layout">
      <button className="menu-toggle" aria-expanded={menuOpen} onClick={() => setMenuOpen(!menuOpen)}>
        ☰ Menu<span className="muted"> · {current}</span></button>
      <nav className={`side ${menuOpen ? "side-open" : ""}`} aria-label="Sections">
        {menu.map(([heading, items]) => (
          <div key={heading || "all"} className="side-group">
            {heading && <div className="side-head">{heading}</div>}
            {items.map(([key, label]) => (
              <button key={key} className={`side-item ${tab === key ? "side-on" : ""}`}
                aria-current={tab === key ? "page" : undefined} onClick={() => go(key)}>{label}</button>
            ))}
          </div>
        ))}
      </nav>
      <div className="content">
      {tenant && tenant.status !== "active" && (
        <Message kind="error">This account is suspended: you can see and download everything, but not change it.
          Contact us to restore it.</Message>
      )}
      {tab === "extensions" && <Extensions me={me} />}
      {tab === "routing" && me.role === "admin" && <Routing />}
      {tab === "desk" && me.role === "admin" && <DeskPhones />}
      {tab === "billing" && me.role === "admin" && <Billing />}
      {tab === "contacts" && me.role === "admin" && <Contacts />}
      {tab === "status" && <Status />}
      {tab === "recordings" && <Recordings me={me} />}
      {tab === "staff" && me.role === "admin" && <Staff me={me} />}
      {tab === "sites" && me.role === "admin" && <SitesPage />}
      {tab === "microsoft" && me.role === "admin" && <MicrosoftPage notice={notice} />}
      {tab === "account" && <Account me={me} onChanged={refresh} />}
      </div>
      </div>
    </Frame>
  );
}
