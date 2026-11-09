// The Phone System's tenant switcher. A cookie picks the tenant every page
// works on (backend main.py, _as_tenant); none is the main tenant. Every
// change also says which tenant the page was showing, from the config it
// loaded, so a page left open from before a switch (another tab) can't
// save into the wrong tenant: the server refuses it.

let shown = null; // the tenant id the page last loaded its config for

export function setShownTenant(id) {
  shown = id;
}

export function tenantHeaders() {
  return shown == null ? {} : { "X-PBX-Tenant": String(shown) };
}

export function chooseTenant(id) {
  document.cookie = `pbx_tenant=${encodeURIComponent(id)}; path=/; SameSite=Strict`;
}

export function forgetTenant() {
  document.cookie = "pbx_tenant=; path=/; max-age=0; SameSite=Strict";
}
