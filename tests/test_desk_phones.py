"""Yealink desk phones: per-phone provisioning credentials bound to the
MAC, the phone's tenant's login and hostname, busy lamps for its own
tenant's extensions only."""
import base64

import pytest
from fastapi.testclient import TestClient

from test_portal_caller import PORTAL, two  # noqa: F401  (main tenant 201-203 and Acme 201-202)

MAC, MAC2 = "805ec0123456", "805ec0abcdef"


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def phones(two):
    main, pbx, op, portal, acme = two
    code = pbx.tenant_names(pbx.get_db(), acme).code
    acme_op = TestClient(main.app)
    acme_op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": str(acme)})
    acme_op.cookies.set("pbx_tenant", str(acme))  # the console's tenant switcher
    assert op.post("/api/pbx/desk-phones", json={"mac": "80:5E:C0:12:34:56", "ext": "201", "model": "T46G"}).status_code == 200
    assert acme_op.post("/api/pbx/desk-phones", json={"mac": MAC2, "ext": "202", "model": "T46G"}).status_code == 200
    pw = {r["mac"]: r["prov_pass"] for r in pbx.get_db().execute("SELECT mac, prov_pass FROM desk_phones")}
    return main, pbx, op, acme_op, acme, code, pw


def fetch(main, mac, creds=None):
    return TestClient(main.app).get(f"/prov/yealink/{mac}.cfg", headers=creds or {})


def test_challenge_then_config(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    r = fetch(main, MAC)
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    r = fetch(main, MAC, basic(MAC, pw[MAC]))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    cfg = r.text
    ext = dict(pbx.get_db().execute("SELECT * FROM extensions WHERE tenant_id = 1 AND ext = '201'").fetchone())
    assert "account.1.user_name = 201" in cfg and f"account.1.password = {ext['password']}" in cfg
    assert "account.1.sip_server.1.transport_type = 2" in cfg and "account.1.srtp_encryption = 2" in cfg
    # busy lamps: the main tenant's other extensions only
    assert "linekey.2.value = 202" in cfg and "linekey.3.value = 203" in cfg and "linekey.4.type = 0" in cfg
    assert "linekey.2.pickup_value = **202" in cfg
    assert "static.security.trust_certificates = 1" in cfg
    assert "static.security.cn_validation = 1" in cfg


def test_tenant_phone(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    cfg = fetch(main, MAC2, basic(MAC2, pw[MAC2])).text
    assert f"account.1.user_name = {code}202" in cfg and f"account.1.auth_name = {code}202" in cfg
    assert "linekey.2.value = 201" in cfg and "linekey.3.type = 0" in cfg  # Acme's 201, nothing of the main tenant's
    assert "203" not in cfg.split("account.1.password")[1].split("\n", 1)[1]


def test_credentials_bound_to_the_phone(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    assert fetch(main, MAC2, basic(MAC, pw[MAC])).status_code == 401  # one phone's credentials, another's file
    assert fetch(main, MAC, basic(MAC, "wrong")).status_code == 401
    assert fetch(main, MAC, basic(MAC2, pw[MAC2])).status_code == 401
    assert "refused" in open(pbx.CHANGES_LOG_PATH).read()
    assert fetch(main, "y000000000028").status_code == 404 and fetch(main, f"{MAC}-local").status_code == 404


def test_too_many_failures(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    import deskphones
    deskphones._failures.clear()
    for _ in range(deskphones.MAX_FAILURES):
        assert fetch(main, MAC, basic(MAC, "wrong")).status_code == 401
    assert fetch(main, MAC, basic(MAC, pw[MAC])).status_code == 429  # even the right ones, for a while
    deskphones._failures.clear()


def test_disabled_extension_logs_the_phone_out(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    assert op.put("/api/pbx/extensions/201", json={"name": "T1 201", "enabled": False}).status_code == 200
    cfg = fetch(main, MAC, basic(MAC, pw[MAC])).text
    assert "account.1.enable = 0" in cfg and "account.1.password" not in cfg


def test_each_tenant_sees_its_own_phones(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    assert [p["mac"] for p in op.get("/api/pbx/desk-phones").json()["phones"]] == [MAC]
    assert [p["mac"] for p in acme_op.get("/api/pbx/desk-phones").json()["phones"]] == [MAC2]
    assert op.delete(f"/api/pbx/desk-phones/{MAC2}").status_code == 404
    assert op.get(f"/api/pbx/desk-phones/{MAC2}/config.cfg").status_code == 404
    assert acme_op.post("/api/pbx/desk-phones", json={"mac": MAC, "ext": "201"}).status_code == 409  # unique on the server
    assert acme_op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "203"}).status_code == 400  # not Acme's


def test_checking_copy_hides_passwords(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    shown = op.get(f"/api/pbx/desk-phones/{MAC}/config.cfg").text
    assert "account.1.password = ********" in shown and "user_password = admin:********" in shown
    assert pw[MAC] not in shown


def test_portal_sees_only_its_tenants_phones(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    portal = TestClient(main.app)
    portal.headers.update({"X-Portal-Secret": PORTAL, "X-Portal-Tenant": code})
    r = portal.get("/api/pbx/desk-phones")
    assert r.status_code == 200 and [p["mac"] for p in r.json()["phones"]] == [MAC2]
    assert portal.get(f"/api/pbx/desk-phones/{MAC}/config.cfg").status_code == 404  # the main tenant's
    assert portal.delete(f"/api/pbx/desk-phones/{MAC}").status_code == 404


def test_one_phone_per_extension(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    r = op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "201", "model": "T46G"})
    assert r.status_code == 409 and "already on extension 201" in r.json()["detail"]
    assert op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "202", "model": "T46G"}).status_code == 200
    assert op.put("/api/pbx/desk-phones/805ec0000001", json={"ext": "201", "model": "T46G"}).status_code == 409
    assert op.put(f"/api/pbx/desk-phones/{MAC}", json={"ext": "201", "model": "T41P"}).status_code == 200  # itself


def test_server_url_for_desk_phones(phones, monkeypatch):
    main, pbx, op, acme_op, acme, code, pw = phones
    import deskphones
    assert op.get("/api/pbx/desk-phones").json()["server_url"] == "https://pbx.example/prov/yealink/"  # falls back
    monkeypatch.setattr(deskphones, "DESKPHONE_BASE_URL", "https://pbx.other.example")
    d = op.get("/api/pbx/desk-phones").json()
    assert d["server_url"] == "https://pbx.other.example/prov/yealink/" and d["ready"]


def test_another_tenants_mac_is_not_revealed(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    r = acme_op.post("/api/pbx/desk-phones", json={"mac": MAC, "ext": "201", "model": "T46G"})
    assert r.status_code == 409 and "already" not in r.json()["detail"] and "support" in r.json()["detail"]
    r = op.post("/api/pbx/desk-phones", json={"mac": MAC, "ext": "203", "model": "T46G"})
    assert r.status_code == 409 and "already added" in r.json()["detail"]


def test_failure_table_forgets_expired_ips(phones, monkeypatch):
    main, pbx, op, acme_op, acme, code, pw = phones
    import deskphones
    deskphones._failures.clear()
    fetch(main, MAC, basic(MAC, "wrong"))
    assert len(deskphones._failures) == 1
    fetch(main, MAC, basic(MAC, pw[MAC]))  # a good fetch adds nothing
    assert len(deskphones._failures) == 1
    monkeypatch.setattr(deskphones, "FAILURE_WINDOW", -1)  # everything has expired
    assert fetch(main, MAC, basic(MAC, pw[MAC])).status_code == 200
    assert deskphones._failures == {}


def test_tenant_hostname_for_provisioning(phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    host = "acme.sip.example.ie"
    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "status": "active", "sip_host": host}).status_code == 200
    assert acme_op.get("/api/pbx/desk-phones").json()["server_url"] == f"https://{host}/prov/yealink/"
    assert op.get("/api/pbx/desk-phones").json()["server_url"] == "https://pbx.example/prov/yealink/"  # no hostname
    c = TestClient(main.app)
    # its own hostname, or the shared one: served
    assert c.get(f"/prov/yealink/{MAC2}.cfg", headers={**basic(MAC2, pw[MAC2]), "Host": host}).status_code == 200
    assert c.get(f"/prov/yealink/{MAC2}.cfg", headers={**basic(MAC2, pw[MAC2]), "Host": "pbx.example"}).status_code == 200
    # the main tenant's phone under Acme's hostname: refused
    assert c.get(f"/prov/yealink/{MAC}.cfg", headers={**basic(MAC, pw[MAC]), "Host": host}).status_code == 401
