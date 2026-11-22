"""A new tenant's first portal admin, invited from the console: gwphones
asks the portal on the same server, with the secret they share; the
portal's /internal/ routes refuse anything else, and anything that came
through nginx."""
from fastapi.testclient import TestClient

from conftest import PORTAL_ORIGIN
from test_portal_phone import world  # noqa: F401  (the two-tenant world)


def wire(world, monkeypatch):
    import portal_admins
    from gwportal import app as portal_app
    p = TestClient(portal_app.app, base_url="http://127.0.0.1:8020")

    def send(method, path, json, headers):
        return p.request(method, path, json=json, headers=headers)
    monkeypatch.setattr(portal_admins, "_send", send)
    return p


def test_invite_first_admin_from_the_console(world, monkeypatch):
    wire(world, monkeypatch)
    op, pbx = world["op"], world["pbx"]
    r = op.post("/api/pbx/tenants", json={"name": "NewCo", "outbound_cid": "015558100"})
    tid, code = r.json()["id"], r.json()["code"]
    assert op.get(f"/api/pbx/tenants/{tid}/portal-people").json()["people"] == []
    r = op.post(f"/api/pbx/tenants/{tid}/portal-admins", json={"email": "Boss@NewCo.example", "name": "Boss"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "email": "boss@newco.example"}
    to, subject, body = world["sent"][-1]
    assert to == "boss@newco.example" and "NewCo" in body and f"{PORTAL_ORIGIN}/invite#" in body
    people = op.get(f"/api/pbx/tenants/{tid}/portal-people").json()["people"]
    assert people == [{"email": "boss@newco.example", "name": "Boss", "role": "admin", "ext": None, "invited": True,
                       "disabled": False, "microsoft": False}]
    # the link instead, to pass on by hand
    r = op.post(f"/api/pbx/tenants/{tid}/portal-admins", json={"email": "two@newco.example", "give_link": True})
    assert r.json()["link"].startswith(f"{PORTAL_ORIGIN}/invite#")
    # an address already used anywhere: refused, saying nothing about whose
    assert op.post(f"/api/pbx/tenants/{tid}/portal-admins", json={"email": "ann@acme.example"}).status_code == 409
    # a suspended tenant: refused
    op.put(f"/api/pbx/tenants/{tid}", json={"name": "NewCo", "status": "suspended"})
    assert op.post(f"/api/pbx/tenants/{tid}/portal-admins", json={"email": "x@newco.example"}).status_code == 409
    # the main tenant isn't on the portal
    assert op.post("/api/pbx/tenants/1/portal-admins", json={"email": "y@x.example"}).status_code == 400


def test_internal_routes_refuse_everyone_else(world, monkeypatch):
    p = wire(world, monkeypatch)
    code = world["acme"]["code"]
    assert p.get(f"/internal/tenants/{code}/people").status_code == 404  # no secret
    assert p.get(f"/internal/tenants/{code}/people", headers={"X-Portal-Secret": "wrong"}).status_code == 404
    ok = {"X-Portal-Secret": "portal-test-secret"}
    assert p.get(f"/internal/tenants/{code}/people", headers=ok).status_code == 200
    # through nginx (X-Real-IP set): refused even with the secret
    assert p.get(f"/internal/tenants/{code}/people", headers={**ok, "X-Real-IP": "8.8.8.8"}).status_code == 404
    assert p.post(f"/internal/tenants/{code}/admins", json={"email": "z@acme.example"},
                  headers={**ok, "X-Forwarded-For": "8.8.8.8"}).status_code == 404


def test_not_for_the_portal_caller(world):
    import main
    p = TestClient(main.app)
    p.headers.update({"X-Portal-Secret": "portal-test-secret", "X-Portal-Tenant": world["acme"]["code"]})
    assert p.get(f"/api/pbx/tenants/{world['acme']['id']}/portal-people").status_code == 403
