"""
gwphones: the phone platform's API (working name, until the brand is
chosen).

Serves the operator console (Phone System and Billing) under /api/pbx/
and /api/billing/, and the public pieces Asterisk and phones need: /prov/
(phone setup links), /call/ (call link pages) and /lcr/ (Asterisk's own
route checks, which only answer this machine). It writes the generated
Asterisk files and reloads Asterisk, so it must run in exactly one place.

Run locally:
    uvicorn main:app --reload --port 8010

See ../README.md for settings, nginx and deploying.
"""

import asyncio
import contextvars
import hmac
import os
import re
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_TOKEN = os.environ.get("GWPHONES_TOKEN", "")
# Sent by nginx (proxy_set_header X-Proxy-Secret ...) on every request it
# lets through after its login, so requests that didn't come through it are
# refused. See "Auth" below and ../README.md.
PROXY_SECRET = os.environ.get("GWPHONES_PROXY_SECRET", "")
# Local development only: with neither secret set, everything is refused
# unless this is "1".
ALLOW_NO_AUTH = os.environ.get("GWPHONES_ALLOW_NO_AUTH", "") == "1"
CORS_ORIGINS = [o for o in os.environ.get("GWPHONES_CORS_ORIGINS", "").split(",") if o]
# The tenant portal's own credential (X-Portal-Secret): a separate, much
# narrower caller than the operator, see "The tenant portal" below.
PORTAL_SECRET = os.environ.get("GWPHONES_PORTAL_SECRET", "")


# ---------------------------------------------------------------------------
# The tenant portal (portal/) calls this API for a tenant's staff, as a
# caller of its own. It may only use the routes below, matched by method
# and route template after routing (never the raw path, so .., // or
# encoded slashes can't get round it); anything else is refused. Its tenant
# comes only from X-Portal-Tenant, by its code (1002; the operator's
# X-PBX-Tenant and switcher cookie mean nothing here); an unknown tenant
# is refused and a suspended one is read-only (it keeps its right to
# download recordings). It never gets tenants, settings, trunks, routes,
# apply, billing or raw passwords. X-Portal-User (who's acting) only goes
# into the change log.
# ---------------------------------------------------------------------------

PORTAL_ROUTES = {
    ("GET", "/api/pbx/tenant"),
    ("GET", "/api/pbx/extensions"),
    ("POST", "/api/pbx/extensions"),
    ("PUT", "/api/pbx/extensions/{ext}"),
    ("POST", "/api/pbx/extensions/{ext}/reset-password"),
    ("POST", "/api/pbx/extensions/{ext}/reset-voicemail-pin"),
    ("POST", "/api/pbx/extensions/{ext}/setup-link"),
    ("POST", "/api/pbx/extensions/{ext}/email-setup"),
    ("PUT", "/api/pbx/extensions/{ext}/voicemail-email"),
    ("GET", "/api/pbx/forwarding"),
    ("PUT", "/api/pbx/extensions/{ext}/forwarding"),
    ("GET", "/api/pbx/status/tenant"),
    ("GET", "/api/pbx/recordings"),
    ("GET", "/api/pbx/recordings/file/{name}"),
    ("DELETE", "/api/pbx/recordings/file/{name}"),
    # Ring groups, IVR menus (and greetings) and business hours: the portal
    # lets only a tenant's admins at these.
    ("GET", "/api/pbx/routing"),
    ("POST", "/api/pbx/ringgroups"),
    ("PUT", "/api/pbx/ringgroups/{number}"),
    ("DELETE", "/api/pbx/ringgroups/{number}"),
    ("POST", "/api/pbx/ivr"),
    ("PUT", "/api/pbx/ivr/{number}"),
    ("DELETE", "/api/pbx/ivr/{number}"),
    ("GET", "/api/pbx/ivr/{number}/greeting"),
    ("PUT", "/api/pbx/ivr/{number}/greeting"),
    ("DELETE", "/api/pbx/ivr/{number}/greeting"),
    ("POST", "/api/pbx/hours"),
    ("PUT", "/api/pbx/hours/{number}"),
    ("PUT", "/api/pbx/hours/{number}/override"),
    ("DELETE", "/api/pbx/hours/{number}"),
    # Where the tenant's own numbers go (only that: adding, taking or
    # removing a number is the operator's, POST /inbound isn't here).
    ("PUT", "/api/pbx/inbound/{did}/target"),
}
_portal_call = contextvars.ContextVar("gw_portal_call", default=False)


def portal_guard(request: Request):
    """Every route's first dependency: a portal request may only reach the
    routes in PORTAL_ROUTES (by the route that matched)."""
    if _portal_call.get():
        route = request.scope.get("route")
        if (request.method, getattr(route, "path", None)) not in PORTAL_ROUTES:
            raise HTTPException(403, "Not available to the portal.")


# No generated API description or docs pages (/openapi.json, /docs, /redoc):
# they'd describe every route to anyone who can reach the service.
app = FastAPI(title="gwphones API", dependencies=[Depends(portal_guard)],
              openapi_url=None, docs_url=None, redoc_url=None)

if CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Auth: every /api/ route except /api/health, checked once in the middleware
# below rather than in each route, so a new route can't be left open by
# mistake. The exceptions outside /api/ are /prov/ (pbx.py), the one-time
# phone setup links behind the QR codes, /call/ (calllinks.py), the public
# call link pages, and /lcr/ (lcr.py), Asterisk's own route checks, which
# only answer this machine.
#
# In production nginx's login comes first, and nginx adds X-Proxy-Secret to
# each request it lets through, so anything reaching this service some
# other way (another service on the machine, a forged request from one) is
# refused. A bearer token (GWPHONES_TOKEN) also works, for scripts. With
# neither set, everything is refused: this service rewrites the phone
# system's config, so it must never be open by accident.
#
# Changes (anything but GET) are also refused when the browser says they
# came from another site, so a page elsewhere can't use the browser's
# remembered login to make them.
# ---------------------------------------------------------------------------

def _matches(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def is_authorized(request: Request) -> bool:
    if PROXY_SECRET and _matches(request.headers.get("x-proxy-secret", ""), PROXY_SECRET):
        return True
    if API_TOKEN and _matches(request.headers.get("authorization", ""), f"Bearer {API_TOKEN}"):
        return True
    return not PROXY_SECRET and not API_TOKEN and ALLOW_NO_AUTH


def is_cross_site(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    # Older browsers send no Sec-Fetch-Site, but do send Origin on changes.
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        return urlsplit(origin).netloc != host
    return False  # not a browser (curl, scripts): nothing to forge


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if "x-portal-secret" in request.headers:
        return await _as_portal(request, call_next)
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        if not is_authorized(request):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS") and is_cross_site(request):
            return JSONResponse({"detail": "Changes can only be made from the console itself."}, status_code=403)
        if request.url.path.startswith(("/api/pbx/", "/api/billing/")):
            return await _as_tenant(request, call_next)
    return await call_next(request)


# The tenant switcher: a cookie names the tenant the pages work on (none =
# the main tenant), and only here, after the checks above, does it count.
# Every change also says which tenant the page was showing (X-PBX-Tenant),
# so one left open on another tenant (in a tab from before a switch) can't
# save into the wrong one.
async def _as_tenant(request: Request, call_next):
    import pbx
    raw = request.cookies.get("pbx_tenant", "")
    tenant = pbx.MAIN_TENANT
    if raw:
        with pbx.get_db() as conn:
            known = raw.isdigit() and conn.execute("SELECT 1 FROM tenants WHERE id = ?", (int(raw),)).fetchone()
        if not known:
            return JSONResponse({"detail": "The tenant chosen no longer exists: choose one again."}, status_code=400)
        tenant = int(raw)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-pbx-tenant") != str(tenant):
        return JSONResponse({"detail": "The tenant was switched in another tab or window: reload this page, then try again."},
                            status_code=409)
    token = pbx._current_tenant.set(tenant)
    try:
        return await call_next(request)
    finally:
        pbx._current_tenant.reset(token)


def _portal_user(value: str) -> str:
    """Who's acting, for the log only: printable, short, one line."""
    return re.sub(r"[^\x20-\x7e]", "", value)[:120].strip()


async def _as_portal(request: Request, call_next):
    if not (PORTAL_SECRET and _matches(request.headers.get("x-portal-secret", ""), PORTAL_SECRET)):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    if "x-proxy-secret" in request.headers or "authorization" in request.headers:
        return JSONResponse({"detail": "One credential per request."}, status_code=403)
    import pbx
    code = request.headers.get("x-portal-tenant", "")
    row = None
    if re.fullmatch(r"[0-9]{4}", code):
        with pbx.get_db() as conn:
            row = conn.execute("SELECT id, status FROM tenants WHERE code = ?", (code,)).fetchone()
    if not row:
        return JSONResponse({"detail": "Unknown tenant."}, status_code=400)
    if row["status"] != "active" and request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": "This account is suspended: you can still see and download, not change."},
                            status_code=403)
    tokens = (_portal_call.set(True), pbx._current_tenant.set(row["id"]),
              pbx._acting_user.set(_portal_user(request.headers.get("x-portal-user", "")) or "unnamed"))
    try:
        return await call_next(request)
    finally:
        pbx._acting_user.reset(tokens[2])
        pbx._current_tenant.reset(tokens[1])
        _portal_call.reset(tokens[0])


# ---------------------------------------------------------------------------
# Background work: rewriting the Asterisk config at start, voicemail emails,
# recording clean-up, billing imports. Off for a test run against copies
# (PBX_DISABLE_BACKGROUND=1): it must only ever run in one place, or emails
# go twice and recordings get moved.
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    if os.environ.get("PBX_DISABLE_BACKGROUND") == "1":
        return
    from pbx import apply_on_startup, voicemail_mail_loop
    from recordings import recordings_expiry_loop
    from billing import billing_loop
    asyncio.create_task(apply_on_startup())
    asyncio.create_task(voicemail_mail_loop())
    asyncio.create_task(recordings_expiry_loop())
    asyncio.create_task(billing_loop())


@app.get("/api/health")
def api_health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# The phone system itself, /api/pbx/*, and phone setup links, /prov/.
from pbx import router as pbx_router, public_router as pbx_public_router  # noqa: E402

app.include_router(pbx_router)
app.include_router(pbx_public_router)

# Call links: /api/pbx/call-links and the public /call/ pages.
from calllinks import router as calllinks_router, public_router as calllinks_public_router  # noqa: E402

app.include_router(calllinks_router)
app.include_router(calllinks_public_router)

# IVR menus and their greetings, business hours and call recordings.
from ivr import router as ivr_router  # noqa: E402
from hours import router as hours_router  # noqa: E402
from recordings import router as recordings_router  # noqa: E402

app.include_router(ivr_router)
app.include_router(hours_router)
app.include_router(recordings_router)

# Billing (call costs from rate sheets), statements, and the price cap and
# cheapest route: /api/billing/*, and /lcr/ for Asterisk.
from billing import router as billing_router  # noqa: E402
from statements import router as statements_router  # noqa: E402
from lcr import public_router as lcr_public_router, router as lcr_router  # noqa: E402

app.include_router(billing_router)
app.include_router(statements_router)
app.include_router(lcr_router)
app.include_router(lcr_public_router)
