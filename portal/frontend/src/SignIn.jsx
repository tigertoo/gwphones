import React, { useEffect, useState } from "react";
import { post, takeFragmentToken } from "./api";
import { Button, Field, Message, useAction } from "./ui";

// Signing in, step by step as the API says: password, then the
// two-factor code (or a recovery code), or for an admin without codes
// yet, setting them up. Also: forgotten password, and the pages an
// invitation or reset link opens (/invite#token, /reset#token).

export function SignIn({ onState }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [forgot, setForgot] = useState(false);
  const a = useAction();

  if (forgot) return <Forgot email={email} onBack={() => setForgot(false)} />;

  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => post("/login", { email, password }));
    if (r) onState(r);
  };
  return (
    <form className="card narrow" onSubmit={submit}>
      <h1>Sign in</h1>
      <Field label="Email">
        <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </Field>
      <Field label="Password">
        <input type="password" autoComplete="current-password" required value={password}
          onChange={(e) => setPassword(e.target.value)} />
      </Field>
      {a.show}
      <Button busy={a.busy} type="submit">Sign in</Button>
      <button type="button" className="link" onClick={() => setForgot(true)}>Forgotten your password?</button>
    </form>
  );
}

export function CodeStep({ onState }) {
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState(false);
  const a = useAction();
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => post("/login/code", recovery ? { recovery_code: code } : { code }));
    if (r) onState(r);
    else setCode("");
  };
  return (
    <form className="card narrow" onSubmit={submit}>
      <h1>Your code</h1>
      <p>{recovery ? "Enter one of your recovery codes. Each works once." : "Enter the 6-digit code from your authenticator app."}</p>
      <Field label={recovery ? "Recovery code" : "Code"}>
        <input autoFocus required inputMode={recovery ? "text" : "numeric"} autoComplete="one-time-code"
          pattern={recovery ? undefined : "[0-9 ]{6,7}"} value={code} onChange={(e) => setCode(e.target.value)} />
      </Field>
      {a.show}
      <Button busy={a.busy} type="submit">Continue</Button>
      <button type="button" className="link" onClick={() => { setRecovery(!recovery); setCode(""); }}>
        {recovery ? "Use a code from the app instead" : "Lost your phone? Use a recovery code"}
      </button>
      <SignOutLink onState={onState} />
    </form>
  );
}

// Setting up two-factor codes: a QR code for the app, a code back from
// it, then the recovery codes, shown once. Used at an admin's first
// sign-in (enrol) and from the Account page.
export function SetUpCodes({ onDone, intro }) {
  const [qr, setQr] = useState(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState(null);
  const a = useAction();
  useEffect(() => {
    a.run(() => post("/2fa/start")).then((r) => r && setQr(r));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (codes) return <RecoveryCodes codes={codes} onDone={onDone} />;
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => post("/2fa/finish", { code }));
    if (r) setCodes(r.recovery_codes);
  };
  const secret = qr ? new URL(qr.uri).searchParams.get("secret") : "";
  return (
    <form className="card narrow" onSubmit={submit}>
      <h1>Two-factor codes</h1>
      {intro && <p>{intro}</p>}
      <ol className="steps">
        <li>In an authenticator app (Microsoft Authenticator, Google Authenticator, 1Password…), add an account by scanning this code.</li>
        <li>Enter the 6-digit code the app shows.</li>
      </ol>
      {qr && <img className="qr" src={qr.qr} alt="QR code for your authenticator app" />}
      {secret && <p className="small">Can't scan it? Enter this key in the app: <code className="key">{secret}</code></p>}
      <Field label="Code from the app">
        <input required inputMode="numeric" autoComplete="one-time-code" pattern="[0-9 ]{6,7}" value={code}
          onChange={(e) => setCode(e.target.value)} />
      </Field>
      {a.show}
      <Button busy={a.busy} type="submit">Turn on</Button>
    </form>
  );
}

export function RecoveryCodes({ codes, onDone }) {
  const [kept, setKept] = useState(false);
  return (
    <div className="card narrow">
      <h1>Your recovery codes</h1>
      <p>If you lose your phone, each of these signs you in once instead of a code. Keep them somewhere safe, away from
        your phone: they won't be shown again.</p>
      <ul className="codes">{codes.map((c) => <li key={c}><code>{c}</code></li>)}</ul>
      <label className="check"><input type="checkbox" checked={kept} onChange={(e) => setKept(e.target.checked)} />
        I've kept them somewhere safe</label>
      <Button disabled={!kept} onClick={onDone}>Continue</Button>
    </div>
  );
}

function Forgot({ email: start, onBack }) {
  const [email, setEmail] = useState(start);
  const [done, setDone] = useState(null);
  const a = useAction();
  const submit = async (e) => {
    e.preventDefault();
    const r = await a.run(() => post("/password/forgot", { email }));
    if (r) setDone(r.message);
  };
  return (
    <form className="card narrow" onSubmit={submit}>
      <h1>Forgotten password</h1>
      {done ? <Message kind="ok">{done}</Message> : (
        <>
          <p>We'll email you a link to choose a new one.</p>
          <Field label="Email">
            <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          {a.show}
          <Button busy={a.busy} type="submit">Email me a link</Button>
        </>
      )}
      <button type="button" className="link" onClick={onBack}>Back to signing in</button>
    </form>
  );
}

// The page an invitation (/invite#token) or reset (/reset#token) link opens.
export function LinkPage({ kind }) {
  const [token] = useState(() => takeFragmentToken());
  const [who, setWho] = useState(null);
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [done, setDone] = useState(false);
  const a = useAction();
  useEffect(() => {
    if (kind === "invite" && token) a.run(() => post("/invite/check", { token })).then((r) => r && setWho(r));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const home = () => window.location.assign("/");
  if (!token) {
    return <div className="card narrow"><h1>This link isn't complete</h1>
      <p>Open the link from your email again, or ask for a new one.</p>
      <Button onClick={home}>Go to signing in</Button></div>;
  }
  if (done) {
    return <div className="card narrow"><h1>All set</h1><p>Your password is saved. Sign in with it now.</p>
      <Button onClick={home}>Sign in</Button></div>;
  }
  const submit = async (e) => {
    e.preventDefault();
    if (password !== again) {
      a.setMsg({ kind: "error", text: "The two passwords aren't the same." });
      return;
    }
    const r = await a.run(() => post(kind === "invite" ? "/invite/accept" : "/password/reset", { token, password }));
    if (r) setDone(true);
  };
  return (
    <form className="card narrow" onSubmit={submit}>
      <h1>{kind === "invite" ? "Welcome" : "Choose a new password"}</h1>
      {kind === "invite" && who && <p>Choose a password for <b>{who.email}</b>.</p>}
      <Field label="Password" hint="At least 12 characters. A few words together is easy to remember.">
        <input type="password" autoComplete="new-password" required minLength={12} value={password}
          onChange={(e) => setPassword(e.target.value)} />
      </Field>
      <Field label="The same again">
        <input type="password" autoComplete="new-password" required value={again} onChange={(e) => setAgain(e.target.value)} />
      </Field>
      {a.show}
      <Button busy={a.busy} type="submit">Save password</Button>
    </form>
  );
}

export function SignOutLink({ onState }) {
  return (
    <button type="button" className="link" onClick={async () => {
      await post("/logout").catch(() => null);
      onState({ state: "none" });
    }}>Sign out</button>
  );
}
