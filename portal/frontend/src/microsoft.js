import { get, post } from "./api";

// Signing in with Microsoft, and the Microsoft 365 link (microsoft.py):
// the API gives the address to go to; Microsoft sends the browser back to
// /?microsoft=<result>, which is read once and cleared from the address.

const RESULTS = {
  signed_in: null,
  linked: { kind: "ok", tab: "staff", text: "Microsoft 365 is connected. Now choose the group to sync, below." },
  consented: { kind: "ok", tab: "staff", text: "Thanks: if your Microsoft 365 admin approved it, Preview and Sync now will work." },
  cancelled: { kind: "info", text: "Signing in with Microsoft was cancelled." },
  expired: { kind: "error", text: "That sign-in took too long or was started elsewhere. Try again." },
  failed: { kind: "error", text: "Microsoft didn't confirm the sign-in. Try again." },
  busy: { kind: "error", text: "Too many attempts from here. Try again in a while." },
  no_account: { kind: "error", text: "There's no portal account for that Microsoft account. Ask your organisation's admin." },
  elsewhere: { kind: "error", tab: "staff", text: "That Microsoft 365 organisation is already connected to another portal account." },
  other_org: { kind: "error", tab: "staff", text: "This portal is connected to a different Microsoft 365 organisation. Disconnect it first." },
  account_taken: { kind: "error", tab: "staff", text: "That Microsoft account is already linked to someone else here." },
};

export function takeMicrosoftResult() {
  const params = new URLSearchParams(window.location.search);
  const key = params.get("microsoft");
  if (!key) return null;
  window.history.replaceState(null, "", window.location.pathname);
  return RESULTS[key] === undefined ? null : RESULTS[key];
}

export const microsoftAvailable = () => get("/microsoft/available").then((r) => r.available).catch(() => false);

export async function goToMicrosoft(path) {
  const r = await post(path);
  window.location.assign(r.url);
}
