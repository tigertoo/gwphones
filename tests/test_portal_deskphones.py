"""The portal's desk phones: a tenant's admins only, its own phones only,
one per extension, and no passwords in the audit log."""
import re

from test_portal_phone import PW, ext_row, world  # noqa: F401  (the two-tenant world)

MAC = "805ec0123456"


def test_staff_refused_everywhere_here(world):
    from gwportal import deskphones
    ann = world["client"]("ann")
    tried = 0
    for route in deskphones.router.routes:
        for method in route.methods - {"HEAD"}:
            path = re.sub(r"\{[^}]+\}", MAC, route.path)
            r = ann.request(method, path, json={"mac": MAC, "ext": "201"})
            assert r.status_code == 403, (method, route.path, r.status_code)
            tried += 1
    assert tried == 6


def test_add_move_and_view(world):
    admin = world["client"]("admin")
    r = admin.post("/api/desk-phones", json={"mac": "80:5E:C0:12:34:56", "ext": "201", "model": "T46G"})
    assert r.status_code == 200 and r.json()["ok"]
    d = admin.get("/api/desk-phones").json()
    p = d["phones"][0]
    assert p["mac"] == MAC and p["ext"] == "201" and p["username"] == MAC and p["password"] and p["admin_password"]
    assert d["server_url"].endswith("/prov/yealink/") and "T46G" in d["models"]
    assert "last_fetch_ip" not in p
    cfg = admin.get(f"/api/desk-phones/{MAC}/config")
    assert cfg.status_code == 200 and "account.1.password = ********" in cfg.text and p["password"] not in cfg.text
    assert cfg.headers["cache-control"] == "no-store"
    assert admin.put(f"/api/desk-phones/{MAC}", json={"ext": "202", "model": "T46G"}).status_code == 200
    assert admin.get("/api/desk-phones").json()["phones"][0]["ext"] == "202"
    # one per extension
    r = admin.post("/api/desk-phones", json={"mac": "805ec0000001", "ext": "202"})
    assert r.status_code == 409 and "its own extension" in r.json()["detail"]
    # a new provisioning password
    assert admin.post(f"/api/desk-phones/{MAC}/new-password").status_code == 200
    assert admin.get("/api/desk-phones").json()["phones"][0]["password"] != p["password"]


def test_other_tenants_phones(world):
    op, acme = world["op"], world["acme"]
    assert op.post("/api/pbx/desk-phones", json={"mac": MAC, "ext": "203", "model": "T46G"}).status_code == 200
    admin = world["client"]("admin")
    assert admin.get("/api/desk-phones").json()["phones"] == []
    assert admin.get(f"/api/desk-phones/{MAC}/config").status_code == 404
    assert admin.delete(f"/api/desk-phones/{MAC}").status_code == 404
    r = admin.post("/api/desk-phones", json={"mac": MAC, "ext": "201"})
    assert r.status_code == 409 and "already" not in r.json()["detail"]


def test_bad_mac_never_reaches_a_path(world):
    admin = world["client"]("admin")
    assert admin.post("/api/desk-phones", json={"mac": "../extensions", "ext": "201"}).status_code == 400
    assert admin.get("/api/desk-phones/zz5ec0123456/config").status_code == 400


def test_remove_with_new_sip_password(world):
    acme = world["acme"]["id"]
    admin = world["client"]("admin")
    admin.post("/api/desk-phones", json={"mac": MAC, "ext": "201", "model": "T46G"})
    before = ext_row(world, acme, "201")["password"]
    assert admin.delete(f"/api/desk-phones/{MAC}?new_sip_password=true").status_code == 200
    assert admin.get("/api/desk-phones").json()["phones"] == []
    assert ext_row(world, acme, "201")["password"] != before
    # without it, the extension keeps its password
    admin.post("/api/desk-phones", json={"mac": MAC, "ext": "202", "model": "T46G"})
    before = ext_row(world, acme, "202")["password"]
    assert admin.delete(f"/api/desk-phones/{MAC}").status_code == 200
    assert ext_row(world, acme, "202")["password"] == before


def test_no_passwords_in_the_audit_log(world):
    admin = world["client"]("admin")
    admin.post("/api/desk-phones", json={"mac": MAC, "ext": "201", "model": "T46G"})
    p = admin.get("/api/desk-phones").json()["phones"][0]
    admin.post(f"/api/desk-phones/{MAC}/new-password")
    p2 = admin.get("/api/desk-phones").json()["phones"][0]
    from gwportal import db
    with db.connect() as conn:
        rows = [" ".join(str(v) for v in tuple(r)) for r in conn.execute("SELECT * FROM audit")]
    text = "\n".join(rows)
    assert "desk phone added" in text
    for secret in (p["password"], p["admin_password"], p2["password"]):
        assert secret not in text


def test_suspended_tenant_can_only_look(world):
    op, acme = world["op"], world["acme"]["id"]
    admin = world["client"]("admin")
    admin.post("/api/desk-phones", json={"mac": MAC, "ext": "201", "model": "T46G"})
    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "status": "suspended"}).status_code == 200
    assert admin.get("/api/desk-phones").status_code == 200
    for method, path in (("POST", "/api/desk-phones"), ("PUT", f"/api/desk-phones/{MAC}"),
                         ("POST", f"/api/desk-phones/{MAC}/new-password"), ("DELETE", f"/api/desk-phones/{MAC}")):
        r = admin.request(method, path, json={"mac": "805ec0000001", "ext": "202"})
        assert r.status_code == 403 and "suspended" in r.json()["detail"], (method, path)
