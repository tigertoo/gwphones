// Every call to the portal's API: same site, its cookie, JSON. An error
// throws with the API's own message (written to be shown as it is).
export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export async function api(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : "Something went wrong. Try again.";
    throw new ApiError(detail, res.status, data);
  }
  return data;
}

export const get = (path) => api("GET", path);
export const post = (path, body = {}) => api("POST", path, body);
export const put = (path, body = {}) => api("PUT", path, body);
export const del = (path) => api("DELETE", path);

// A token handed over in the address's fragment (#...), read once and
// taken out of the address bar.
export function takeFragmentToken() {
  const token = window.location.hash.replace(/^#/, "");
  if (token) window.history.replaceState(null, "", window.location.pathname);
  return token;
}
