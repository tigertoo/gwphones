"""4-digit extensions (2000-8999) for a new tenant, 3 for one from before:
the login's length tells them apart, so older phones keep working, and
every pattern, check and range follows the tenant's own digits."""
import os

from fastapi.testclient import TestClient


def setup(env, monkeypatch):
    import main, pbx
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    r = op.post("/api/pbx/tenants", json={"name": "Big", "outbound_cid": "015557000"})  # 4 digits: the default now
    assert r.status_code == 200, r.text
    tid, code = r.json()["id"], r.json()["code"]
    op.cookies.set("pbx_tenant", str(tid))
    op.headers["X-PBX-Tenant"] = str(tid)
    return op, pbx, tid, code


def test_new_tenant_gets_four_digits(env, monkeypatch):
    op, pbx, tid, code = setup(env, monkeypatch)
    assert op.post("/api/pbx/extensions", json={"ext": "201", "name": "Old style"}).status_code == 400
    assert op.post("/api/pbx/extensions", json={"ext": "9001", "name": "Too high"}).status_code == 400
    assert op.post("/api/pbx/extensions", json={"ext": "2001", "name": "Dublin"}).status_code == 200
    assert op.post("/api/pbx/extensions", json={"ext": "3001", "name": "Cork"}).status_code == 200
    r = op.post("/api/pbx/extensions", json={"name": "Next free"})
    assert r.status_code == 200 and len(r.json()["ext"]) == 4
    cfg = op.get("/api/pbx/config").json()
    assert cfg["limits"]["ext_min"] == 2000 and cfg["limits"]["ext_max"] == 8999 and cfg["limits"]["ext_digits"] == "4"
    pjsip = open(pbx.EXT_CONF).read()
    assert f"[{code}2001]" in pjsip and f"[{code}3001]" in pjsip
    dial = open(os.path.join(pbx.GENERATED_DIR, "extensions_portal.conf")).read()
    block = dial.split(f"[t{code}-call-ext]")[1].split("\n[")[0]
    assert block.startswith("\nexten => _[2-8]XXX,1,")
    internal = dial.split(f"[t{code}-internal]")[1].split("\n[")[0]
    assert "exten => _[2-8]XXX,1,Goto(" in internal
    features = dial.split(f"[t{code}-features]")[1].split("\n[")[0]
    assert 'REGEX("^[2-8][0-9]{3}$" ${ME})' in features and "exten => _**[2-8]XXX,1,PickupChan(" in features
    c2c = dial.split(f"[t{code}-c2c-ring]")[1].split("\n[")[0]
    assert "exten => _[2-8]XXXX.," in c2c and "${EXTEN:0:4}" in c2c
    # the main tenant keeps 3 digits
    main_block = dial.split("[portal-call-ext]")[1].split("\n[")[0]
    assert "exten => _2XX,1," in main_block


def test_logins_of_both_lengths(env):
    import pbx
    assert pbx.parse_login("201") == ("1001", "201")
    assert pbx.parse_login("1002201") == ("1002", "201")
    assert pbx.parse_login("10032001") == ("1003", "2001")
    assert pbx.channel_ext("PJSIP/10032001-0000001a", "1003") == "2001"
    assert pbx.parse_login("10039001") is None and pbx.parse_login("1003101") is None


def test_forwarding_and_click_to_call_to_a_four_digit_extension(env, monkeypatch):
    op, pbx, tid, code = setup(env, monkeypatch)
    op.post("/api/pbx/extensions", json={"ext": "2001", "name": "A"})
    op.post("/api/pbx/extensions", json={"ext": "3001", "name": "B"})
    with pbx.get_db() as conn:
        token = pbx._current_tenant.set(tid)
        try:
            assert pbx.forward_target(conn, "2001", "3001") == "3001"
            assert pbx.click_to_call_target(conn, "2001", "3001") == "3001"
        finally:
            pbx._current_tenant.reset(token)


def test_digits_listed_with_the_tenant(env, monkeypatch):
    op, pbx, tid, code = setup(env, monkeypatch)
    t = next(t for t in op.get("/api/pbx/tenants").json()["tenants"] if t["id"] == tid)
    assert t["settings"]["ext_digits"] == "4"
    assert op.post("/api/pbx/tenants", json={"name": "Odd", "outbound_cid": "015557001", "ext_digits": "5"}).status_code == 400
