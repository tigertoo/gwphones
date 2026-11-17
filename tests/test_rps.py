"""Yealink RPS: the request signing (against the worked examples in
Yealink's "Json API for RPS Management Platform" V3.6.0.20), and desk
phones registered, updated and removed there, against a fake RPS."""
import base64
import hashlib
import hmac
import json

import pytest

from test_desk_phones import MAC, MAC2, basic, phones  # noqa: F401
from test_portal_caller import PORTAL, two  # noqa: F401

KEY, SECRET = "2df23f2d9c255e7138dc603b3847b58a", "d4a4be460a8d43609d8e8a5e7d0d4ad1"


def test_string_to_sign_body_example(env):
    import rps
    headers = {"Content-MD5": "KOK7YpXasjJC+MslP+MGWw==", "X-Ca-Key": KEY, "X-Ca-Nonce": "b681e77450a04d22aaffc914a3379561",
               "X-Ca-Timestamp": "1544008291631"}
    assert rps.string_to_sign("POST", "/api/open/v1/server/list", headers) == (
        "POST\nContent-MD5:KOK7YpXasjJC+MslP+MGWw==\nX-Ca-Key:2df23f2d9c255e7138dc603b3847b58a\n"
        "X-Ca-Nonce:b681e77450a04d22aaffc914a3379561\nX-Ca-Timestamp:1544008291631\napi/open/v1/server/list")


def test_string_to_sign_query_example(env):
    import rps
    headers = {"X-Ca-Key": KEY, "X-Ca-Nonce": "9e730a223b48433785494801fb016d39", "X-Ca-Timestamp": "1544094691000"}
    assert rps.string_to_sign("GET", "/api/open/v1/device/checkMac", headers, {"mac": "001565123123"}) == (
        "GET\nX-Ca-Key:2df23f2d9c255e7138dc603b3847b58a\nX-Ca-Nonce:9e730a223b48433785494801fb016d39\n"
        "X-Ca-Timestamp:1544094691000\napi/open/v1/device/checkMac\nmac=001565123123")
    # keys sorted; an empty value keeps only its name
    assert rps.string_to_sign("GET", "/x", headers, {"b": "2", "a": " "}).endswith("\nx\na&b=2")


class FakeRps:
    """Yealink's service, enough for these tests: checks every signature."""
    def __init__(self):
        self.devices, self.calls, self.fail, self.seq = {}, [], {}, 0

    def __call__(self, method, url, headers, content, query):
        import rps
        path = url.split(":8443", 1)[1]
        assert headers["X-Ca-Key"] == KEY
        if content is not None:
            assert headers["Content-MD5"] == base64.b64encode(hashlib.md5(content).digest()).decode()
        want = base64.b64encode(hmac.new(SECRET.encode(), rps.string_to_sign(method, path, headers, query).encode(),
                                         hashlib.sha256).digest()).decode()
        assert headers["X-Ca-Signature"] == want
        body = json.loads(content) if content else {}
        self.calls.append((path, body))
        op = path.rsplit("/", 1)[1]
        if op in self.fail:
            return Answer({"ret": -1, "data": None, "error": {"msg": self.fail[op], "errorCode": 409}})
        if op == "add":
            mac = body["macs"][0]
            if mac in self.devices:
                return Answer({"ret": -1, "data": None, "error": {"msg": "device.mac.existed", "errorCode": 409}})
            self.seq += 1
            self.devices[mac] = {"id": f"id{self.seq}", "mac": mac, **{k: body[k] for k in ("uniqueServerUrl", "authName", "password")}}
            return Answer({"ret": 1, "data": [{"id": f"id{self.seq}", "mac": mac}], "error": None})
        if path == "/api/open/v1/server/list":
            return Answer({"ret": 1, "data": {"data": []}})
        if op == "checkMac":
            return Answer({"ret": 1, "data": {"existed": query["mac"] in self.devices, "self": None}})
        if op == "checkDevice":
            return Answer({"ret": 1, "data": "Unknown"})
        if op == "list":
            return Answer({"ret": 1, "data": {"data": [d for d in self.devices.values() if body["key"] in d["mac"]]}})
        if op == "edit":
            d = next((d for d in self.devices.values() if d["id"] == body["id"]), None)
            if not d:
                return Answer({"ret": -1, "error": {"msg": "device.not.found", "errorCode": 404}})
            d.update({k: body[k] for k in ("uniqueServerUrl", "authName", "password")})
            return Answer({"ret": 1, "data": {"id": d["id"]}})
        if op == "delete":
            for mac in [m for m, d in self.devices.items() if d["id"] in body["ids"]]:
                del self.devices[mac]
            return Answer({"ret": 0, "data": None, "error": None})
        raise AssertionError(path)


class Answer:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


@pytest.fixture
def fake(phones, monkeypatch):
    import rps
    f = FakeRps()
    monkeypatch.setattr(rps, "ACCESS_KEY", KEY)
    monkeypatch.setattr(rps, "ACCESS_SECRET", SECRET)
    monkeypatch.setattr(rps, "_send", f)
    return f


def test_added_phone_is_registered_with_its_own_credentials(phones, fake):
    main, pbx, op, acme_op, acme, code, pw = phones
    r = op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "202", "model": "T54W"})
    assert r.status_code == 200 and r.json()["rps"]["status"] == "registered" and "plug it in" in r.json()["message"]
    d = fake.devices["805ec0000001"]
    prov = pbx.get_db().execute("SELECT prov_pass FROM desk_phones WHERE mac = '805ec0000001'").fetchone()[0]
    assert d["uniqueServerUrl"] == "https://pbx.example/prov/yealink/" and d["authName"] == "805ec0000001" and d["password"] == prov
    p = next(p for p in op.get("/api/pbx/desk-phones").json()["phones"] if p["mac"] == "805ec0000001")
    assert p["rps_status"] == "registered"
    # a new password goes to RPS
    op.post("/api/pbx/desk-phones/805ec0000001/new-password")
    new = pbx.get_db().execute("SELECT prov_pass FROM desk_phones WHERE mac = '805ec0000001'").fetchone()[0]
    assert fake.devices["805ec0000001"]["password"] == new != prov
    # removed: gone from RPS too
    assert op.delete("/api/pbx/desk-phones/805ec0000001").status_code == 200
    assert "805ec0000001" not in fake.devices


def test_already_ours_is_updated_not_refused(phones, fake):
    main, pbx, op, acme_op, acme, code, pw = phones
    fake.devices["805ec0000001"] = {"id": "old", "mac": "805ec0000001", "uniqueServerUrl": "x", "authName": "x", "password": "x"}
    r = op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "202"})
    assert r.json()["rps"]["status"] == "registered" and fake.devices["805ec0000001"]["authName"] == "805ec0000001"
    # and if it's gone from RPS meanwhile, it's added again
    del fake.devices["805ec0000001"]
    assert op.post("/api/pbx/desk-phones/805ec0000001/rps").json()["registered"]
    assert "805ec0000001" in fake.devices


def test_another_companys_phone_is_saved_but_not_registered(phones, fake):
    main, pbx, op, acme_op, acme, code, pw = phones
    fake.fail["add"] = "device.mac.added.by.other"
    r = op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "202"})
    assert r.status_code == 200 and r.json()["rps"]["status"] == "elsewhere"
    assert "another company" in r.json()["message"]
    p = next(p for p in op.get("/api/pbx/desk-phones").json()["phones"] if p["mac"] == "805ec0000001")
    assert p["rps_status"] == "elsewhere"
    del fake.fail["add"]
    assert op.post("/api/pbx/desk-phones/805ec0000001/rps").json()["rps"]["status"] == "registered"


def test_tenant_phone_gets_its_own_address(phones, fake):
    main, pbx, op, acme_op, acme, code, pw = phones
    op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "status": "active", "sip_host": "acme.sip.example.ie"})
    assert acme_op.post(f"/api/pbx/desk-phones/{MAC2}/rps").status_code == 200
    assert fake.devices[MAC2]["uniqueServerUrl"] == "https://acme.sip.example.ie/prov/yealink/"


def test_without_keys_nothing_is_sent(phones, monkeypatch):
    import rps
    sent = []
    monkeypatch.setattr(rps, "_send", lambda *a: sent.append(a))
    main, pbx, op, acme_op, acme, code, pw = phones
    r = op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "202"})
    assert r.status_code == 200 and r.json()["rps"]["status"] == "" and sent == []
    assert op.post("/api/pbx/desk-phones/805ec0000001/rps").status_code == 400


def test_rps_unreachable_doesnt_stop_the_save(phones, fake, monkeypatch):
    import httpx
    import rps

    def down(*a):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(rps, "_send", down)
    main, pbx, op, acme_op, acme, code, pw = phones
    r = op.post("/api/pbx/desk-phones", json={"mac": "805ec0000001", "ext": "202"})
    assert r.status_code == 200 and r.json()["rps"]["status"] == "failed"
    assert op.delete("/api/pbx/desk-phones/805ec0000001").status_code == 200


def test_a_sync_fits_inside_the_portals_wait(env, portal_env):
    import rps
    from gwportal import gw
    gw.client = None
    wait = gw._client().timeout.read
    assert 3 * rps.TIMEOUT < wait  # add, list, edit, inside the portal's wait for the save


def test_connection_check_is_read_only(phones, fake):
    main, pbx, op, acme_op, acme, code, pw = phones
    r = op.get(f"/api/pbx/rps/check?mac={MAC}")
    assert r.status_code == 200, r.text
    paths = [c[0] for c in fake.calls]
    assert paths == ["/api/open/v1/device/checkMac", "/api/open/v1/device/checkDevice", "/api/open/v1/server/list"]


def test_connection_check_not_for_the_portal(phones, fake):
    from fastapi.testclient import TestClient
    main, pbx, op, acme_op, acme, code, pw = phones
    p = TestClient(main.app)
    p.headers.update({"X-Portal-Secret": PORTAL, "X-Portal-Tenant": code})
    assert p.get(f"/api/pbx/rps/check?mac={MAC}").status_code == 403
