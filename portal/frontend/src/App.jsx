import React, { useCallback, useEffect, useState } from "react";
import { get, post } from "./api";
import { Account } from "./Account";
import { Billing } from "./Billing";
import { DeskPhones } from "./DeskPhones";
import { Extensions, Recordings, Status } from "./Phones";
import { Routing } from "./Routing";
import { CodeStep, LinkPage, SetUpCodes, SignIn } from "./SignIn";
import { Staff } from "./Staff";
import { Message } from "./ui";
import { takeMicrosoftResult } from "./microsoft";

// The portal: what's shown follows the session's state from the API
// (none, pending_mfa, enrol, full). Links from emails open /invite#... or
// /reset#...

const TABS = {
  admin: [["extensions", "Extensions"], ["routing", "Call routing"], ["desk", "Desk phones"], ["status", "Status"],
    ["recordings", "Recordings"], ["billing", "Billing"],
    ["staff", "Staff"], ["account", "Account"]],
  user: [["extensions", "My phone"], ["status", "Status"], ["recordings", "Recordings"], ["account", "Account"]],
};

export default function App() {
  const path = window.location.pathname;
  if (path === "/invite") return <Frame><LinkPage kind="invite" /></Frame>;
  if (path === "/reset") return <Frame><LinkPage kind="reset" /></Frame>;
  return <Portal />;
}

function Frame({ tenant, children, right }) {
  return (
    <div className="app">
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
  const tabs = TABS[me.role] || TABS.user;
  const signOut = async () => {
    await post("/logout").catch(() => null);
    setSession({ state: "none" });
    setTenant(null);
    setTab("extensions");
  };
  return (
    <Frame tenant={tenant} right={<button className="link light" onClick={signOut}>Sign out</button>}>
      <nav className="tabs" aria-label="Sections">
        {tabs.map(([key, label]) => (
          <button key={key} className={`tab ${tab === key ? "tab-on" : ""}`} aria-current={tab === key ? "page" : undefined}
            onClick={() => setTab(key)}>{label}</button>
        ))}
      </nav>
      {tenant && tenant.status !== "active" && (
        <Message kind="error">This account is suspended: you can see and download everything, but not change it.
          Contact us to restore it.</Message>
      )}
      {tab === "extensions" && <Extensions me={me} />}
      {tab === "routing" && me.role === "admin" && <Routing />}
      {tab === "desk" && me.role === "admin" && <DeskPhones />}
      {tab === "billing" && me.role === "admin" && <Billing />}
      {tab === "status" && <Status />}
      {tab === "recordings" && <Recordings me={me} />}
      {tab === "staff" && me.role === "admin" && <Staff me={me} notice={notice} />}
      {tab === "account" && <Account me={me} onChanged={refresh} />}
    </Frame>
  );
}
