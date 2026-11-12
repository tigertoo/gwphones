"""The tenant portal as a caller of gwphones (main.py, PORTAL_ROUTES): its
own secret, a fixed tenant, an allow-list by matched route, read-only when
suspended, and nothing of another tenant reachable even where both have
the same extension numbers."""
import os
import re

import pytest
from fastapi.testclient import TestClient

PORTAL = "portal-test-secret"
REC = "20261101-100000_201_0871111111_in_1793000000.{}.ulaw"


@pytest.fixture
def two(env, monkeypatch):
    """Main tenant (1) with 201-203 and Acme with 201-202: both have a 201."""
    monkeypatch.setenv("GWPHONES_PORTAL_SECRET", PORTAL)
    import main, pbx
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    assert op.post("/api/pbx/tenants", json={"name": "Acme", "outbound_cid": "015559999"}).status_code == 200
    conn = pbx.get_db()
    acme = conn.execute("SELECT id FROM tenants WHERE name = 'Acme'").fetchone()["id"]
    for tid, exts in ((1, ("201", "202", "203")), (acme, ("201", "202"))):
        token = pbx._current_tenant.set(tid)
        try:
            for e in exts:
                pbx.api_create_extension(pbx.ExtensionCreate(ext=e, name=f"T{tid} {e}"))
        finally:
            pbx._current_tenant.reset(token)
    code = conn.execute("SELECT code FROM tenants WHERE id = ?", (acme,)).fetchone()["code"]
    portal = TestClient(main.app)
    portal.headers.update({"X-Portal-Secret": PORTAL, "X-Portal-Tenant": code, "X-Portal-User": "alice@acme.example"})
    return main, pbx, op, portal, acme


def row(pbx, tid, ext):
    return dict(pbx.get_db().execute("SELECT * FROM extensions WHERE tenant_id = ? AND ext = ?", (tid, ext)).fetchone())


def concrete(path):
    """A route template with sample values for its parameters."""
    return re.sub(r"\{[^}]+\}", "201", path)


# -- who may call --------------------------------------------------------------

def test_wrong_or_unset_portal_secret(two, monkeypatch):
    main, pbx, op, portal, acme = two
    assert portal.get("/api/pbx/extensions", headers={"X-Portal-Secret": "nope"}).status_code == 401
    monkeypatch.setattr(main, "PORTAL_SECRET", "")
    assert portal.get("/api/pbx/extensions").status_code == 401


def test_both_credentials_refused(two):
    main, pbx, op, portal, acme = two
    assert portal.get("/api/pbx/extensions", headers={"Authorization": "Bearer testtoken"}).status_code == 403
    assert portal.get("/api/pbx/extensions", headers={"X-Proxy-Secret": "x"}).status_code == 403


@pytest.mark.parametrize("tenant", ["", "abc", "999", "1", "2", "1002 ", "10022", "-100", "9999"])
def test_tenant_code_required_and_known(two, tenant):
    main, pbx, op, portal, acme = two
    assert portal.get("/api/pbx/extensions", headers={"X-Portal-Tenant": tenant}).status_code == 400


def test_operator_tenant_header_and_cookie_ignored(two):
    main, pbx, op, portal, acme = two
    portal.cookies.set("pbx_tenant", "1")
    exts = portal.get("/api/pbx/extensions", headers={"X-PBX-Tenant": "1"}).json()["extensions"]
    assert [e["ext"] for e in exts] == ["201", "202"] and all(e["name"].startswith(f"T{acme} ") for e in exts)
    # the operator's header alone names no tenant for the portal
    assert TestClient(main.app).get("/api/pbx/extensions", headers={
        "X-Portal-Secret": PORTAL, "X-PBX-Tenant": str(acme)}).status_code == 400


def test_own_tenant(two):
    main, pbx, op, portal, acme = two
    assert portal.get("/api/pbx/tenant").json() == {"code": portal.headers["X-Portal-Tenant"], "name": "Acme",
                                                     "status": "active"}


def test_operator_unaffected(two):
    main, pbx, op, portal, acme = two
    assert op.get("/api/pbx/config").status_code == 200


# -- the allow-list --------------------------------------------------------------

def test_every_other_route_refused(two):
    """Every route in the app that isn't on the list, with any method, is
    403 for the portal."""
    main, pbx, op, portal, acme = two
    tried = 0
    for route in main.app.routes:
        for method in getattr(route, "methods", None) or ():
            if (method, route.path) in main.PORTAL_ROUTES or method == "HEAD":
                continue
            r = portal.request(method, concrete(route.path), json={})
            assert r.status_code == 403, (method, route.path, r.status_code)
            tried += 1
    assert tried > 40


@pytest.mark.parametrize("path", ["/api/pbx/../pbx/config", "//api/pbx/config", "/api/pbx/%2e%2e/pbx/config",
                                  "/api/pbx/extensions/201/../../config", "/api/pbx/extensions/201/credentials",
                                  "/api/pbx/extensions/%2e%2e/credentials", "/api/pbx/status"])
def test_path_tricks(two, path):
    main, pbx, op, portal, acme = two
    r = portal.get(path, follow_redirects=True)
    assert r.status_code in (403, 404, 405), (path, r.status_code)
    assert "password" not in r.text and "tenants" not in r.text


def test_list_without_secrets(two):
    main, pbx, op, portal, acme = two
    for e in portal.get("/api/pbx/extensions").json()["extensions"]:
        assert set(e) == {"ext", "name", "enabled", "created_at", "vm_email", "record", "vm_nopin"}


# -- the right tenant's 201, never the other's -----------------------------------------

def test_rename_and_switch_off(two):
    main, pbx, op, portal, acme = two
    before = row(pbx, 1, "201")
    assert portal.put("/api/pbx/extensions/201", json={"name": "Front", "enabled": False}).status_code == 200
    assert row(pbx, acme, "201")["name"] == "Front" and not row(pbx, acme, "201")["enabled"]
    assert row(pbx, 1, "201") == before


def test_create_in_own_tenant(two):
    main, pbx, op, portal, acme = two
    r = portal.post("/api/pbx/extensions", json={"name": "New"})
    assert r.status_code == 200 and r.json()["ext"] == "203"
    assert row(pbx, acme, "203")["name"] == "New" and row(pbx, 1, "203")["name"] == "T1 203"


@pytest.mark.parametrize("action,column", [("reset-password", "password"), ("reset-voicemail-pin", "vm_pin")])
def test_new_password_and_pin(two, action, column):
    main, pbx, op, portal, acme = two
    mine, theirs = row(pbx, acme, "201")[column], row(pbx, 1, "201")[column]
    r = portal.post(f"/api/pbx/extensions/201/{action}")
    assert r.status_code == 200 and column not in r.text
    assert row(pbx, acme, "201")[column] != mine and row(pbx, 1, "201")[column] == theirs


def test_voicemail_email(two):
    main, pbx, op, portal, acme = two
    assert portal.put("/api/pbx/extensions/201/voicemail-email", json={"email": "a@acme.example"}).status_code == 200
    assert row(pbx, acme, "201")["vm_email"] == "a@acme.example" and row(pbx, 1, "201")["vm_email"] == ""


def test_setup_link_is_the_tenants(two):
    main, pbx, op, portal, acme = two
    r = portal.post("/api/pbx/extensions/201/setup-link", json={"app": "groundwire"})
    assert r.status_code == 200
    tokens = pbx.get_db().execute("SELECT ext, tenant_id FROM provision_tokens").fetchall()
    assert [(t["ext"], t["tenant_id"]) for t in tokens] == [("201", acme)]
    assert portal.post("/api/pbx/extensions/203/setup-link", json={}).status_code == 404  # only tenant 1 has 203


def test_setup_email_is_the_tenants(two, monkeypatch):
    main, pbx, op, portal, acme = two
    sent = []
    monkeypatch.setattr(pbx, "_graph_ready", lambda: True)
    monkeypatch.setattr(pbx, "_graph_send", lambda to, subject, body, images: sent.append((to, subject, body)))
    r = portal.post("/api/pbx/extensions/201/email-setup", json={"to": "a@acme.example", "apps": ["groundwire"]})
    assert r.status_code == 200 and len(sent) == 1
    assert row(pbx, acme, "201")["vm_pin"] in sent[0][2] and row(pbx, 1, "201")["vm_pin"] not in sent[0][2]
    tokens = pbx.get_db().execute("SELECT tenant_id FROM provision_tokens").fetchall()
    assert [t["tenant_id"] for t in tokens] == [acme]


def test_forwarding_other_tenants_extension(two):
    main, pbx, op, portal, acme = two
    assert portal.put("/api/pbx/extensions/203/forwarding", json={"dnd": True}).status_code == 404


def test_recordings_are_the_tenants(two):
    main, pbx, op, portal, acme = two
    conn = pbx.get_db()
    mine, theirs = pbx.tenant_names(conn, acme).rec_dir, pbx.tenant_names(conn, 1).rec_dir
    for folder in (mine, theirs):
        os.makedirs(folder, exist_ok=True)
    open(os.path.join(mine, REC.format(1)), "wb").write(b"\xff" * 800)
    open(os.path.join(theirs, REC.format(1)), "wb").write(b"\x7f" * 800)  # same name, other tenant
    open(os.path.join(theirs, REC.format(2)), "wb").write(b"\x7f" * 800)
    names = [r["name"] for r in portal.get("/api/pbx/recordings").json()["recordings"]]
    assert names == [REC.format(1)]
    assert portal.get(f"/api/pbx/recordings/file/{REC.format(2)}").status_code == 404
    for name in ("..%2Ft1001%2F" + REC.format(2), "%2e%2e%2f" + REC.format(2)):
        assert portal.get(f"/api/pbx/recordings/file/{name}").status_code == 404
    assert portal.delete(f"/api/pbx/recordings/file/{REC.format(1)}").status_code == 200
    assert not os.path.exists(os.path.join(mine, REC.format(1))) and os.path.exists(os.path.join(theirs, REC.format(1)))


def test_tenant_status_only_its_own(two, monkeypatch):
    main, pbx, op, portal, acme = two
    code = pbx.tenant_names(pbx.get_db(), acme).code
    fake = {"asterisk_running": True, "checked_at": "now", "trunks": {"irishvoip": {}},
            "extensions": {"201": {"status": "Avail", "address": "1.2.3.4"}},
            "channels": [{"channel": f"PJSIP/{code}201-0000001", "state": "Up", "caller_id": "0871111111",
                          "duration_seconds": 5, "recording": False},
                         {"channel": "PJSIP/201-0000002", "state": "Up", "caller_id": "0872222222",
                          "duration_seconds": 9, "recording": False},
                         {"channel": "PJSIP/irishvoip-0000003", "state": "Up", "caller_id": "0871111111",
                          "duration_seconds": 5, "recording": False}]}
    monkeypatch.setattr(pbx, "get_status", lambda t: fake)
    s = portal.get("/api/pbx/status/tenant").json()
    assert [c["ext"] for c in s["calls"]] == ["201"] and s["calls"][0]["caller_id"] == "0871111111"
    assert "trunks" not in s and "address" not in str(s["phones"])


# -- suspended: read-only ---------------------------------------------------------------

def test_suspended_is_read_only(two):
    main, pbx, op, portal, acme = two
    conn = pbx.get_db()
    folder = pbx.tenant_names(conn, acme).rec_dir
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, REC.format(1)), "wb").write(b"\xff" * 800)
    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "status": "suspended"}).status_code == 200
    assert portal.get("/api/pbx/extensions").status_code == 200
    assert portal.get(f"/api/pbx/recordings/file/{REC.format(1)}").status_code == 200
    assert portal.put("/api/pbx/extensions/201", json={"name": "X", "enabled": True}).status_code == 403
    assert portal.delete(f"/api/pbx/recordings/file/{REC.format(1)}").status_code == 403


# -- who did it -------------------------------------------------------------------------

def test_change_log_names_the_portal_user(two):
    main, pbx, op, portal, acme = two
    portal.put("/api/pbx/extensions/201", json={"name": "Front", "enabled": True},
               headers={"X-Portal-User": "bob@acme.example\r\nforged line"})
    log = open(pbx.CHANGES_LOG_PATH).read()
    assert "Updated extension 201: name=Front" in log
    line = [l for l in log.splitlines() if "name=Front" in l][0]
    assert "[portal: bob@acme.exampleforged line]" in line  # one line, the header's line break gone
    op.put("/api/pbx/extensions/202", headers={"X-PBX-Tenant": "1"}, json={"name": "Op", "enabled": True})
    assert "[portal:" not in [l for l in open(pbx.CHANGES_LOG_PATH).read().splitlines() if "name=Op" in l][0]


# -- forwarding lives in Asterisk's own database (AstDB), keyed by login -------------------

@pytest.fixture
def astdb(two, monkeypatch):
    """A fake Asterisk answering `database show/put/del`, holding both
    tenants' keys: the main tenant's 201 is bare, Acme's is code + 201."""
    main, pbx, op, portal, acme = two
    code = pbx.tenant_names(pbx.get_db(), acme).code
    db = {("CF", "201"): "0871111111", ("DND", "202"): "1", ("CF", f"{code}202"): "0872222222"}
    writes = []

    def cli(command, timeout=8):
        parts = command.split()
        if parts[:2] == ["database", "show"]:
            return "\n".join(f"/{f}/{k} : {v}" for (f, k), v in sorted(db.items())) + f"\n{len(db)} results found.\n"
        if parts[:2] == ["database", "put"]:
            writes.append(("put", parts[2], parts[3]))
            db[(parts[2], parts[3])] = parts[4]
            return "Updated database successfully"
        if parts[:2] == ["database", "del"]:
            writes.append(("del", parts[2], parts[3]))
            db.pop((parts[2], parts[3]), None)
            return "Database entry removed."
        return ""
    monkeypatch.setattr(pbx, "asterisk_cli", cli)
    return db, writes, code


def test_forwarding_read_is_the_tenants(two, astdb):
    main, pbx, op, portal, acme = two
    db, writes, code = astdb
    mine = portal.get("/api/pbx/forwarding").json()["extensions"]
    assert mine == {"202": {"dnd": False, "always": "0872222222", "busy": "", "no_answer": ""}}
    theirs = op.get("/api/pbx/forwarding").json()["extensions"]
    assert set(theirs) == {"201", "202"} and theirs["201"]["always"] == "0871111111" and theirs["202"]["dnd"]


def test_forwarding_write_is_the_tenants(two, astdb):
    main, pbx, op, portal, acme = two
    db, writes, code = astdb
    r = portal.put("/api/pbx/extensions/201/forwarding", json={"dnd": True, "always": "0873333333"})
    assert r.status_code == 200, r.text
    assert writes and all(key == f"{code}201" for _, _, key in writes)  # never the bare 201
    assert db[("DND", f"{code}201")] == "1" and db[("CF", f"{code}201")] == "0873333333"
    assert db[("CF", "201")] == "0871111111" and ("DND", "201") not in db  # the main tenant's 201 untouched
