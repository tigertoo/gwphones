import React, { useCallback, useEffect, useState } from "react";
import { get, post } from "./api";
import { RecoveryCodes, SetUpCodes } from "./SignIn";
import { Button, Field, useAction } from "./ui";

// One's own account: password, two-factor codes, recovery codes.

export function Account({ me, onChanged }) {
  const [mode, setMode] = useState(null); // setup | codes
  const [codes, setCodes] = useState(null);
  if (mode === "setup") return <SetUpCodes onDone={() => { setMode(null); onChanged(); }} />;
  if (codes) return <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />;
  return (
    <section>
      <h2>My account</h2>
      <p>{me.name ? `${me.name}, ` : ""}{me.email} · {me.role === "admin" ? "Admin" : `Extension ${me.ext || "–"}`}</p>
      <ChangePassword />
      <div className="card">
        <h3>Two-factor codes</h3>
        {me.two_factor ? (
          <>
            <p className="small">On: signing in asks for a code from your authenticator app.</p>
            <TrustedDevices />
            <NewRecoveryCodes onCodes={setCodes} />
            {me.role !== "admin" && <TurnOff onDone={onChanged} />}
          </>
        ) : (
          <>
            <p className="small">Off. With them on, a stolen password alone can't sign in as you.</p>
            <Button kind="secondary" onClick={() => setMode("setup")}>Turn on</Button>
          </>
        )}
      </div>
    </section>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const a = useAction();
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => post("/password/change", { current, new: next }),
      "Password changed. You're signed out everywhere else.");
    if (r) {
      setCurrent("");
      setNext("");
    }
  };
  return (
    <form className="card" onSubmit={submit}>
      <h3>Password</h3>
      <Field label="Current password">
        <input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
      </Field>
      <Field label="New password" hint="At least 12 characters.">
        <input type="password" autoComplete="new-password" required minLength={12} value={next} onChange={(e) => setNext(e.target.value)} />
      </Field>
      {a.show}
      <Button busy={a.busy} kind="secondary" type="submit">Change password</Button>
    </form>
  );
}

function NewRecoveryCodes({ onCodes }) {
  const [code, setCode] = useState("");
  const a = useAction();
  return (
    <div className="block">
      <Field label="New recovery codes" hint="The old ones stop working. Enter a current code from your app.">
        <div className="inline-form">
          <input inputMode="numeric" autoComplete="one-time-code" placeholder="Code" value={code} onChange={(e) => setCode(e.target.value)} />
          <Button busy={a.busy} kind="secondary" onClick={async () => {
            const r = await a.run(() => post("/2fa/recovery-codes", { code }));
            if (r) onCodes(r.recovery_codes);
          }}>Make new ones</Button>
        </div>
      </Field>
      {a.show}
    </div>
  );
}

function TurnOff({ onDone }) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const a = useAction();
  return (
    <div className="block">
      <h3>Turn off</h3>
      <Field label="Your password">
        <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
      </Field>
      <Field label="A current code"><input inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)} /></Field>
      {a.show}
      <Button busy={a.busy} kind="danger" onClick={async () => {
        const r = await a.run(() => post("/2fa/off", { password, code }), "Two-factor codes turned off.");
        if (r) onDone();
      }}>Turn off two-factor codes</Button>
    </div>
  );
}

// Devices that don't ask for a code (chosen at the code step).
function TrustedDevices() {
  const [d, setD] = useState(null);
  const a = useAction();
  const load = useCallback(() => get("/devices").then(setD).catch(() => setD(null)), []);
  useEffect(() => { load(); }, [load]);
  if (!d) return null;
  const forget = async (everywhere) => {
    const r = await a.run(() => post("/devices/forget", { everywhere }),
      everywhere ? "Every device will ask for a code again." : "This device will ask for a code again.");
    if (r) load();
  };
  return (
    <div className="block">
      <p className="small">
        {d.count === 0 ? "Every device asks for a code."
          : `${d.count === 1 ? "1 device doesn't" : `${d.count} devices don't`} ask for a code (up to ${d.days} days from when you chose it)`
            + (d.this_device ? ", this one included." : ".")}
      </p>
      {d.count > 0 && (
        <div className="buttons">
          {d.this_device && <Button kind="secondary" busy={a.busy} onClick={() => forget(false)}>Ask on this device again</Button>}
          <Button kind="secondary" busy={a.busy} onClick={() => forget(true)}>Ask on every device again</Button>
        </div>
      )}
      {a.show}
    </div>
  );
}
