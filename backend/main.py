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
import hmac
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
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

app = FastAPI(title="gwphones API")

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
