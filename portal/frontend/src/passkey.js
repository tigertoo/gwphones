import { post } from "./api";

// Passkeys (WebAuthn) in the browser. The server sends its options as
// JSON with base64url strings; the browser wants ArrayBuffers, and its
// answer goes back as base64url again. Converted by hand, so it works in
// browsers without PublicKeyCredential.parseCreationOptionsFromJSON.

export const passkeysAvailable = () => typeof window !== "undefined" && !!window.PublicKeyCredential
  && !!navigator.credentials;

function toBuf(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)).buffer;
}

function toB64u(buf) {
  if (!buf) return null;
  let s = "";
  new Uint8Array(buf).forEach((b) => { s += String.fromCharCode(b); });
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const descriptors = (list) => (list || []).map((d) => ({ ...d, id: toBuf(d.id) }));

// The browser's own "cancelled" (or timed out): nothing to report.
export const cancelled = (e) => e && (e.name === "NotAllowedError" || e.name === "AbortError");

export async function addPasskey(password, name) {
  const start = await post("/passkeys/register/start", { password });
  const o = start.options;
  const cred = await navigator.credentials.create({
    publicKey: { ...o, challenge: toBuf(o.challenge), user: { ...o.user, id: toBuf(o.user.id) },
      excludeCredentials: descriptors(o.excludeCredentials) },
  });
  const r = cred.response;
  return post("/passkeys/register/finish", {
    challenge_id: start.challenge_id,
    name,
    credential: {
      id: cred.id, rawId: toB64u(cred.rawId), type: cred.type, clientExtensionResults: cred.getClientExtensionResults(),
      response: { clientDataJSON: toB64u(r.clientDataJSON), attestationObject: toB64u(r.attestationObject),
        transports: r.getTransports ? r.getTransports() : [] },
    },
  });
}

export async function signInWithPasskey() {
  const start = await post("/passkey/login/start");
  const o = start.options;
  const cred = await navigator.credentials.get({
    publicKey: { ...o, challenge: toBuf(o.challenge), allowCredentials: descriptors(o.allowCredentials) },
  });
  const r = cred.response;
  return post("/passkey/login/finish", {
    challenge_id: start.challenge_id,
    credential: {
      id: cred.id, rawId: toB64u(cred.rawId), type: cred.type, clientExtensionResults: cred.getClientExtensionResults(),
      response: { clientDataJSON: toB64u(r.clientDataJSON), authenticatorData: toB64u(r.authenticatorData),
        signature: toB64u(r.signature), userHandle: toB64u(r.userHandle) },
    },
  });
}
