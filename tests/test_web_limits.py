"""Browser calls (call links) are limited to protect the server and other
tenants, not by plan: per link (2), per tenant (web_max, its own), and
over all tenants (web_max_total). They never count towards a tenant's
calls at once, which counts PSTN calls only."""
import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def links(env):
    import main, pbx, calllinks
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    assert op.post("/api/pbx/tenants", json={"name": "Acme", "outbound_cid": "015559999", "web_max": 4}).status_code == 200
    acme = pbx.get_db().execute("SELECT id, code FROM tenants WHERE name = 'Acme'").fetchone()
    for tid in (1, acme["id"]):
        token = pbx._current_tenant.set(tid)
        try:
            pbx.api_create_extension(pbx.ExtensionCreate(ext="201", name="Desk"))
            calllinks.api_add_call_link(calllinks.CallLinkRequest(label=f"T{tid} site", target="201"))
        finally:
            pbx._current_tenant.reset(token)
    return pbx, op, acme


def entry(gen, label):
    return gen.split(f": {label})")[1].split("exten =>")[0]


def test_each_tenants_own_limit(links):
    pbx, op, acme = links
    gen = open(pbx.PORTAL_DIALPLAN).read()
    main_link, acme_link = entry(gen, "T1 site"), entry(gen, f"T{acme['id']} site")
    assert "GROUP(webtenant)=1001" in main_link and "GROUP_COUNT(1001@webtenant)} > 10]" in main_link
    assert f"GROUP(webtenant)={acme['code']}" in acme_link and f"GROUP_COUNT({acme['code']}@webtenant)}} > 4]" in acme_link
    for e in (main_link, acme_link):
        assert "GROUP_COUNT(all@webguests)} > 30]" in e and "> 2]?busy" in e  # all tenants; per link
        assert "tenantcalls" not in e  # not the plan's calls at once


def test_changing_them(links):
    pbx, op, acme = links
    assert op.put(f"/api/pbx/tenants/{acme['id']}", json={"name": "Acme", "web_max": 6}).status_code == 200
    s = op.get("/api/pbx/config").json()["settings"]
    assert op.put("/api/pbx/settings", json={**s, "web_max_total": 50}).status_code == 200
    gen = open(pbx.PORTAL_DIALPLAN).read()
    acme_link = entry(gen, f"T{acme['id']} site")
    assert f"GROUP_COUNT({acme['code']}@webtenant)}} > 6]" in acme_link and "@webguests)} > 50]" in acme_link
    assert "GROUP_COUNT(1001@webtenant)} > 10]" in entry(gen, "T1 site")  # the main tenant's untouched
    listed = {t["code"]: t["settings"]["web_max"] for t in op.get("/api/pbx/tenants").json()["tenants"]}
    assert listed == {"1001": "10", acme["code"]: "6"}
    # leaving it out of an edit keeps it
    assert op.put(f"/api/pbx/tenants/{acme['id']}", json={"name": "Acme Ltd"}).status_code == 200
    assert f"> 6]" in entry(open(pbx.PORTAL_DIALPLAN).read(), f"T{acme['id']} site")


@pytest.mark.parametrize("value", [0, 51, -1])
def test_tenant_limit_checked(links, value):
    pbx, op, acme = links
    assert op.put(f"/api/pbx/tenants/{acme['id']}", json={"name": "Acme", "web_max": value}).status_code == 400
    assert op.post("/api/pbx/tenants", json={"name": "Other", "outbound_cid": "015558888", "web_max": value}).status_code == 400


@pytest.mark.parametrize("value", [0, 201])
def test_server_limit_checked(links, value):
    pbx, op, acme = links
    s = op.get("/api/pbx/config").json()["settings"]
    assert op.put("/api/pbx/settings", json={**s, "web_max_total": value}).status_code == 400
