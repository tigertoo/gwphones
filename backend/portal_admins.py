"""A tenant's portal people, from the console: invite its first admin
(the invitation emailed, or the link given back to pass on), and see who
it has. gwphones asks the portal on the same server
(PBX_PORTAL_INTERNAL_URL, http://127.0.0.1:8020) at its /internal/
routes, which aren't on the website, with the secret the two share
(GWPHONES_PORTAL_SECRET). Operator only: not in main.PORTAL_ROUTES."""
import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pbx import MAIN_TENANT, changes_log, get_db

router = APIRouter(prefix="/api/pbx/tenants")
PORTAL_URL = os.environ.get("PBX_PORTAL_INTERNAL_URL", "http://127.0.0.1:8020").rstrip("/")
TIMEOUT = 20

_send = None  # replaced in tests: (method, path, json) -> response


def _portal(method: str, path: str, json: Optional[dict] = None):
    secret = os.environ.get("GWPHONES_PORTAL_SECRET", "")
    if not secret:
        raise HTTPException(503, "The portal isn't linked to this server (GWPHONES_PORTAL_SECRET).")
    try:
        if _send:
            r = _send(method, path, json, {"X-Portal-Secret": secret})
        else:
            with httpx.Client(timeout=TIMEOUT) as c:
                r = c.request(method, PORTAL_URL + path, json=json, headers={"X-Portal-Secret": secret})
        data = r.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "The portal can't be reached on this server.")
    if r.status_code >= 400:
        raise HTTPException(r.status_code if r.status_code in (400, 404, 409, 503) else 502,
                            str(data.get("detail") or "The portal refused it.")[:300])
    return data


def _tenant(tenant_id: int):
    if tenant_id == MAIN_TENANT:
        raise HTTPException(400, "The main tenant isn't on the portal.")
    with get_db() as conn:
        r = conn.execute("SELECT * FROM tenants WHERE id = ? AND status != 'deleted'", (tenant_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Tenant not found.")
    return r


@router.get("/{tenant_id}/portal-people")
def api_portal_people(tenant_id: int):
    t = _tenant(tenant_id)
    return _portal("GET", f"/internal/tenants/{t['code']}/people")


class PortalAdminRequest(BaseModel):
    email: str
    name: str = ""
    give_link: bool = False


@router.post("/{tenant_id}/portal-admins")
def api_invite_portal_admin(tenant_id: int, req: PortalAdminRequest):
    t = _tenant(tenant_id)
    r = _portal("POST", f"/internal/tenants/{t['code']}/admins", req.dict())
    changes_log.info(f"Portal admin {r.get('email')} invited for tenant {t['code']} ({t['name']})"
                     + (": link given to the operator" if req.give_link else ": invitation emailed"))
    return r
