"""Tenants deleted in gwphones (the console): their people go from the
portal too. Every hour (and at start) each tenant code with people here is
asked about; when gwphones answers "Unknown tenant." (nothing else: not a
timeout, not "suspended"), that tenant's logins, sessions, passkeys,
trusted devices, links and recovery codes are removed, with its Microsoft
365 link. The audit log keeps what they did."""
import asyncio
import logging
import os

from . import auth, db, gw
from .app import app

log = logging.getLogger("gwportal")
CHECK_SECONDS = 3600
GONE = "Unknown tenant."


def tenant_gone(code: str) -> bool:
    try:
        gw.call({"tenant_code": code, "email": "portal-cleanup"}, "GET", "/tenant")
        return False
    except auth.Refused as e:
        return e.status == 400 and str(e) == GONE


def remove_tenant(conn, code: str) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE tenant_code = ?", (code,))]
    for uid in ids:
        for table in ("sessions", "links", "recovery_codes", "trusted_devices", "passkeys", "webauthn_challenges"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM users WHERE tenant_code = ?", (code,))
    conn.execute("DELETE FROM ms_links WHERE tenant_code = ?", (code,))
    conn.execute("DELETE FROM ms_switched_off WHERE tenant_code = ?", (code,))
    db.audit(conn, "tenant deleted in gwphones: people removed", {"id": None, "email": "portal-cleanup",
                                                                 "tenant_code": code}, "", f"{len(ids)} people")
    return len(ids)


def purge_gone_tenants() -> dict:
    with db.connect() as conn:
        codes = sorted({r["tenant_code"] for r in conn.execute("SELECT DISTINCT tenant_code FROM users")})
    removed = {}
    for code in codes:
        if tenant_gone(code):
            with db.connect() as conn:
                removed[code] = remove_tenant(conn, code)
            log.warning("Tenant %s is gone from gwphones: %d people removed from the portal", code, removed[code])
    return removed


async def gone_loop():
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(purge_gone_tenants)
        except Exception:
            log.exception("Checking for deleted tenants failed")
        await asyncio.sleep(CHECK_SECONDS)


@app.on_event("startup")
async def _start_gone():
    if os.environ.get("PORTAL_DISABLE_BACKGROUND") != "1":
        asyncio.get_event_loop().create_task(gone_loop())
