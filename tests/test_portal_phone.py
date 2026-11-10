"""The portal's phone actions and staff, against the real gwphones app run
in the same process: the main tenant (1001) and Acme (1002) both have a
201. Acme has an admin, a user on its 201 and a user on its 202; the
main tenant has a user on its own 201."""
import os

import pyotp
import pytest
from fastapi.testclient import TestClient

from conftest import PORTAL_ORIGIN

PW = "correct horse battery"
import time as _time
# Recording names end with Asterisk's uniqueid: the call's time (UTC epoch).
REC = "20261101-100000_{ext}_0871111111_in_{epoch}.{n}.ulaw"


def now_epoch():
    """A call time just after the test's extensions were made: taken when
    the test runs (not when this file loads, which on a slow machine can be
    a minute or more earlier)."""
    return int(_time.time()) + 5


@pytest.fixture
def world(env, portal_env, monkeypatch):
    monkeypatch.setenv("GWPHONES_PORTAL_SECRET", "portal-test-secret")
    import main, pbx
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    assert op.post("/api/pbx/tenants", json={"name": "Acme", "outbound_cid": "015559999"}).status_code == 200
    conn = pbx.get_db()
    acme = conn.execute("SELECT id, code FROM tenants WHERE name = 'Acme'").fetchone()
    for tid, exts in ((1, ("201", "202", "203")), (acme["id"], ("201", "202"))):
        token = pbx._current_tenant.set(tid)
        try:
            for e in exts:
                pbx.api_create_extension(pbx.ExtensionCreate(ext=e, name=f"T{tid} {e}"))
        finally:
            pbx._current_tenant.reset(token)

    from gwportal import app as portal_app, auth, db, gw, mail, phone
    gw.client = TestClient(main.app, base_url="http://gw")
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr(mail, "ready", lambda: True)
    phone._saves.clear()
    phone._status.clear()

    created = {(r["tenant_id"], r["ext"]): r["created_at"] for r in conn.execute("SELECT tenant_id, ext, created_at FROM extensions")}
    secret = pyotp.random_base32()
    people = {}
    with db.connect() as pconn:
        for key, email, code, role, tid, ext in (
                ("admin", "admin@acme.example", acme["code"], "admin", acme["id"], None),
                ("ann", "ann@acme.example", acme["code"], "user", acme["id"], "201"),
                ("bob", "bob@acme.example", acme["code"], "user", acme["id"], "202"),
                ("main", "zoe@main.example", "1001", "user", 1, "201")):
            u = portal_app.create_user(pconn, email, code, role, key.title(), ext, created.get((tid, ext)))
            pconn.execute("UPDATE users SET pw_hash = ?, totp_secret = ? WHERE id = ?",
                          (auth.hash_password(PW), auth._fernet().encrypt(secret.encode()).decode() if role == "admin"
                           else None, u["id"]))
            people[key] = email

    def client_for(key):
        c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
        r = c.post("/api/login", json={"email": people[key], "password": PW})
        if r.json()["state"] == "pending_mfa":
            assert c.post("/api/login/code", json={"code": pyotp.TOTP(secret).now()}).status_code == 200
        return c
    return {"op": op, "pbx": pbx, "acme": acme, "client": client_for, "sent": sent, "phone": phone, "gw": gw,
            "people": people, "portal_app": portal_app}


def ext_row(w, tid, ext):
    return dict(w["pbx"].get_db().execute("SELECT * FROM extensions WHERE tenant_id = ? AND ext = ?", (tid, ext)).fetchone())


# -- what each person sees --------------------------------------------------------------------

def test_admin_sees_the_tenant_only(world):
    a = world["client"]("admin")
    exts = a.get("/api/extensions").json()["extensions"]
    assert [e["ext"] for e in exts] == ["201", "202"] and all(e["name"].startswith(f"T{world['acme']['id']} ") for e in exts)
    assert set(exts[0]) == {"ext", "name", "enabled", "vm_email", "record"}
    assert a.get("/api/tenant").json() == {"code": world["acme"]["code"], "name": "Acme", "status": "active"}


def test_user_sees_only_their_own(world):
    ann = world["client"]("ann")
    assert [e["ext"] for e in ann.get("/api/extensions").json()["extensions"]] == ["201"]
    zoe = world["client"]("main")
    assert [e["name"] for e in zoe.get("/api/extensions").json()["extensions"]] == ["T1 201"]


# -- what a user may and may not do -----------------------------------------------------------------

def test_user_limits(world):
    ann = world["client"]("ann")
    acme = world["acme"]["id"]
    assert ann.put("/api/extensions/202", json={"name": "Mine now"}).status_code == 404
    assert ann.post("/api/extensions/202/new-pin").status_code == 404
    assert ann.post("/api/extensions/202/setup-link", json={}).status_code == 404
    assert ann.post("/api/extensions", json={"name": "Extra"}).status_code == 403
    assert ann.put("/api/extensions/201", json={"name": "Ann", "enabled": False}).status_code == 403
    assert ann.get("/api/users").status_code == 403
    r = ann.put("/api/extensions/201", json={"name": "Ann"})
    assert r.status_code == 200 and r.json()["ok"]
    assert ext_row(world, acme, "201")["name"] == "Ann" and ext_row(world, acme, "201")["enabled"]
    assert ext_row(world, 1, "201")["name"] == "T1 201"


def test_user_setup_email_goes_to_their_own_address(world, monkeypatch):
    pbx = world["pbx"]
    mails = []
    monkeypatch.setattr(pbx, "_graph_ready", lambda: True)
    monkeypatch.setattr(pbx, "_graph_send", lambda to, subject, body, images: mails.append(to))
    ann = world["client"]("ann")
    r = ann.post("/api/extensions/201/setup-email", json={"apps": ["groundwire"], "to": "attacker@evil.example"})
    assert r.status_code == 200 and mails == ["ann@acme.example"]


def test_user_voicemail_email_own_address_only(world):
    ann = world["client"]("ann")
    assert ann.put("/api/extensions/201/voicemail-email", json={"email": "someone@else.example"}).status_code == 403
    assert ann.put("/api/extensions/201/voicemail-email", json={"email": "ANN@acme.example"}).status_code == 200
    assert ext_row(world, world["acme"]["id"], "201")["vm_email"] == "ann@acme.example"


def test_saves_not_yet_live_say_so(world):
    # The tests' Asterisk never reloads, so every save is saved-but-pending.
    r = world["client"]("admin").post("/api/extensions/201/new-password")
    assert r.status_code == 200 and r.json()["pending"] and "password" not in str(r.json()).replace("New password", "")


# -- recordings -------------------------------------------------------------------------------------

def test_recordings(world):
    pbx, acme = world["pbx"], world["acme"]["id"]
    folder = pbx.tenant_names(pbx.get_db(), acme).rec_dir
    os.makedirs(folder, exist_ok=True)
    now = now_epoch()
    mine, bobs = REC.format(ext="201", n=1, epoch=now), REC.format(ext="202", n=2, epoch=now)
    for name in (mine, bobs):
        open(os.path.join(folder, name), "wb").write(b"\xff" * 800)
    ann, admin = world["client"]("ann"), world["client"]("admin")
    assert [r["name"] for r in ann.get("/api/recordings").json()["recordings"]] == [mine]
    assert sorted(r["name"] for r in admin.get("/api/recordings").json()["recordings"]) == sorted([mine, bobs])
    listing = admin.get("/api/recordings").text
    assert '"dir"' not in listing and folder not in listing  # the server's folder isn't passed on
    assert ann.get(f"/api/recordings/{bobs}").status_code == 404
    r = ann.get(f"/api/recordings/{mine}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    # deleting: admins only, and only after the warning
    r = ann.delete(f"/api/recordings/{mine}?confirm=true")
    assert r.status_code == 403 and "admins" in r.json()["detail"] and os.path.exists(os.path.join(folder, mine))
    warning = admin.delete(f"/api/recordings/{bobs}")
    assert warning.status_code == 409 and warning.json()["confirm_needed"] and "can't be undone" in warning.json()["detail"]
    assert os.path.exists(os.path.join(folder, bobs))
    assert admin.delete(f"/api/recordings/{bobs}?confirm=true").status_code == 200
    assert not os.path.exists(os.path.join(folder, bobs))


# -- forwarding and status ------------------------------------------------------------------------------

def test_forwarding_filtered(world, monkeypatch):
    pbx, code = world["pbx"], world["acme"]["code"]
    db = {("CF", f"{code}201"): "0871111111", ("CF", f"{code}202"): "0872222222"}
    monkeypatch.setattr(pbx, "asterisk_cli", lambda cmd, timeout=8: "\n".join(
        f"/{f}/{k} : {v}" for (f, k), v in db.items()) if cmd == "database show" else "")
    ann = world["client"]("ann")
    assert ann.get("/api/forwarding").json()["extensions"] == {
        "201": {"dnd": False, "always": "0871111111", "busy": "", "no_answer": ""}}
    assert set(world["client"]("admin").get("/api/forwarding").json()["extensions"]) == {"201", "202"}


def test_status_filtered_and_cached(world, monkeypatch):
    pbx, code = world["pbx"], world["acme"]["code"]
    asked = []

    def status(t):
        asked.append(1)
        return {"asterisk_running": True, "checked_at": "now", "trunks": {}, "extensions": {
            "201": {"status": "Avail"}, "202": {"status": "Unavailable"}},
            "channels": [{"channel": f"PJSIP/{code}201-1", "state": "Up", "caller_id": "0871111111",
                          "duration_seconds": 3, "recording": False},
                         {"channel": f"PJSIP/{code}202-2", "state": "Ring", "caller_id": "0872222222",
                          "duration_seconds": 1, "recording": False}]}
    monkeypatch.setattr(pbx, "get_status", status)
    ann = world["client"]("ann")
    s = ann.get("/api/status").json()
    assert list(s["phones"]) == ["201"] and [c["ext"] for c in s["calls"]] == ["201"]
    admin = world["client"]("admin")
    assert [c["ext"] for c in admin.get("/api/status").json()["calls"]] == ["201", "202"]
    assert len(asked) == 1  # the second within 5 seconds came from the cache


# -- the binding --------------------------------------------------------------------------------------------

def test_binding_ends_when_the_extension_is_replaced(world):
    op, acme = world["op"], world["acme"]["id"]
    ann = world["client"]("ann")
    assert ann.get("/api/extensions").status_code == 200
    op.cookies.set("pbx_tenant", str(acme))
    assert op.delete("/api/pbx/extensions/201", headers={"X-PBX-Tenant": str(acme)}).status_code == 200
    import time
    time.sleep(1.1)  # created_at has one-second resolution
    assert op.post("/api/pbx/extensions", headers={"X-PBX-Tenant": str(acme)},
                   json={"ext": "201", "name": "Someone new"}).status_code == 200
    r = ann.get("/api/extensions")
    assert r.status_code == 403 and "changed or been removed" in r.json()["detail"]
    assert ann.get("/api/me").status_code == 401  # signed out
    from gwportal import db
    with db.connect() as conn:
        assert conn.execute("SELECT ext FROM users WHERE email = 'ann@acme.example'").fetchone()["ext"] is None


# -- staff ------------------------------------------------------------------------------------------------------

def test_invite_staff(world):
    admin = world["client"]("admin")
    assert admin.post("/api/users", json={"email": "cara@acme.example", "role": "user"}).status_code == 400  # no ext
    assert admin.post("/api/users", json={"email": "cara@acme.example", "role": "user", "ext": "203"}).status_code == 400
    r = admin.post("/api/users", json={"email": "Cara@Acme.example", "name": "Cara", "role": "user", "ext": "202"})
    assert r.status_code == 200 and r.json()["user"]["invited"]
    to, subject, body = world["sent"][-1]
    assert to == "cara@acme.example" and "Acme" in body and f"{PORTAL_ORIGIN}/invite#" in body
    assert admin.post("/api/users", json={"email": "zoe@main.example", "role": "admin"}).status_code == 409
    emails = [u["email"] for u in admin.get("/api/users").json()["users"]]
    assert "zoe@main.example" not in emails and "cara@acme.example" in emails


def test_other_tenants_staff_out_of_reach(world):
    from gwportal import db
    with db.connect() as conn:
        zoe = conn.execute("SELECT id FROM users WHERE email = 'zoe@main.example'").fetchone()["id"]
    admin = world["client"]("admin")
    assert admin.put(f"/api/users/{zoe}", json={"disabled": True}).status_code == 404
    assert admin.post(f"/api/users/{zoe}/invite-again").status_code == 404


def test_last_admin(world):
    from gwportal import db
    with db.connect() as conn:
        me = conn.execute("SELECT id FROM users WHERE email = 'admin@acme.example'").fetchone()["id"]
        ann = conn.execute("SELECT id FROM users WHERE email = 'ann@acme.example'").fetchone()["id"]
    admin = world["client"]("admin")
    assert admin.put(f"/api/users/{me}", json={"role": "user", "ext": "201"}).status_code == 400
    assert admin.put(f"/api/users/{me}", json={"disabled": True}).status_code == 400
    assert admin.put(f"/api/users/{ann}", json={"role": "admin"}).status_code == 200
    assert admin.put(f"/api/users/{me}", json={"disabled": True}).status_code == 200  # now Ann is an admin too


def test_disabling_signs_them_out(world):
    from gwportal import db
    bob = world["client"]("bob")
    with db.connect() as conn:
        bob_id = conn.execute("SELECT id FROM users WHERE email = 'bob@acme.example'").fetchone()["id"]
    assert world["client"]("admin").put(f"/api/users/{bob_id}", json={"disabled": True}).status_code == 200
    assert bob.get("/api/me").status_code == 401


# -- limits and errors -------------------------------------------------------------------------------------------

def test_save_limit_per_tenant(world):
    admin, zoe = world["client"]("admin"), world["client"]("main")
    for i in range(20):
        assert admin.put("/api/extensions/202", json={"name": f"N{i}"}).status_code == 200
    assert admin.put("/api/extensions/202", json={"name": "One too many"}).status_code == 429
    assert zoe.put("/api/extensions/201", json={"name": "Zoe"}).status_code == 200  # another tenant's own limit


def test_suspended_read_only(world):
    op, acme = world["op"], world["acme"]["id"]
    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "status": "suspended"}).status_code == 200
    admin = world["client"]("admin")
    assert admin.get("/api/extensions").status_code == 200
    r = admin.put("/api/extensions/201", json={"name": "X"})
    assert r.status_code == 403 and "suspended" in r.json()["detail"]
    assert admin.post("/api/users", json={"email": "new@acme.example", "role": "admin"}).status_code == 403


def test_gwphones_words_not_passed_on(world):
    # gwphones' own message here names its .env: the portal says something of its own
    r = world["client"]("admin").post("/api/extensions/201/setup-email", json={"apps": ["groundwire"], "to": "a@acme.example"})
    assert r.status_code == 503 and ".env" not in r.text and "MS_GRAPH" not in r.text
    assert r.json()["detail"].startswith("Emails can't be sent right now")


def test_gwphones_down(world, monkeypatch):
    import httpx
    admin = world["client"]("admin")

    class Down:
        cookies = httpx.Cookies()

        def request(self, *a, **k):
            raise httpx.ConnectError("refused")
    monkeypatch.setattr(world["gw"], "client", Down())
    r = admin.get("/api/extensions")
    assert r.status_code == 503 and "can't be reached" in r.json()["detail"]


def test_who_did_it_reaches_gwphones_log(world):
    pbx = world["pbx"]
    world["client"]("ann").put("/api/extensions/201", json={"name": "Ann's phone"})
    line = [l for l in open(pbx.CHANGES_LOG_PATH).read().splitlines() if "Ann's phone" in l][0]
    assert "[portal: ann@acme.example]" in line


def test_earlier_holders_recordings_stay_hidden(world):
    """A recording left by an earlier extension with the same number (made
    before this one was created) isn't the user's, though the admin sees it."""
    pbx, acme = world["pbx"], world["acme"]["id"]
    folder = pbx.tenant_names(pbx.get_db(), acme).rec_dir
    os.makedirs(folder, exist_ok=True)
    now = now_epoch()
    old, new = REC.format(ext="201", n=7, epoch=now - 86400 * 30), REC.format(ext="201", n=8, epoch=now)
    for name in (old, new):
        open(os.path.join(folder, name), "wb").write(b"\xff" * 800)
    ann, admin = world["client"]("ann"), world["client"]("admin")
    assert [r["name"] for r in ann.get("/api/recordings").json()["recordings"]] == [new]
    assert ann.get(f"/api/recordings/{old}").status_code == 404
    assert os.path.exists(os.path.join(folder, old))
    assert old in [r["name"] for r in admin.get("/api/recordings").json()["recordings"]]


PUBLIC = {("GET", "/api/session"), ("POST", "/api/login"), ("POST", "/api/logout"), ("POST", "/api/login/code"),
          ("POST", "/api/password/forgot"), ("POST", "/api/password/reset"), ("POST", "/api/invite/check"),
          ("POST", "/api/invite/accept"), ("POST", "/api/2fa/start"), ("POST", "/api/2fa/finish")}


def test_every_route_needs_a_full_session(world):
    """Default-deny, enumerated: every portal route outside the public set
    refuses no session (401) and a half-signed-in one (403)."""
    import re
    from gwportal import app as portal_app
    pending = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    assert pending.post("/api/login", json={"email": "admin@acme.example", "password": PW}).json()["state"] == "pending_mfa"
    nobody = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    tried = 0
    for route in portal_app.app.routes:
        for method in getattr(route, "methods", None) or ():
            if method == "HEAD" or (method, route.path) in PUBLIC or not route.path.startswith("/api/"):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            assert nobody.request(method, path, json={}).status_code == 401, (method, route.path)
            assert pending.request(method, path, json={}).status_code == 403, (method, route.path)
            tried += 1
    assert tried >= 20


def test_saved_and_live(world, monkeypatch):
    monkeypatch.setattr(world["pbx"], "apply_config", lambda conn, why: {"ok": True, "messages": []})
    r = world["client"]("admin").put("/api/extensions/202", json={"name": "Live"})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_promoted_user_must_set_up_codes(world):
    from gwportal import app as portal_app, db
    with db.connect() as conn:
        bob = conn.execute("SELECT id FROM users WHERE email = 'bob@acme.example'").fetchone()["id"]
    assert world["client"]("admin").put(f"/api/users/{bob}", json={"role": "admin"}).status_code == 200
    c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    assert c.post("/api/login", json={"email": "bob@acme.example", "password": PW}).json()["state"] == "enrol"


def test_failed_invitation_leaves_nothing(world, monkeypatch):
    from gwportal import mail

    def fail(*a):
        raise mail.NotSent("Microsoft Graph said 500")
    monkeypatch.setattr(mail, "send", fail)
    admin = world["client"]("admin")
    assert admin.post("/api/users", json={"email": "dan@acme.example", "role": "admin"}).status_code == 503
    assert "dan@acme.example" not in [u["email"] for u in admin.get("/api/users").json()["users"]]


def test_forwarding_cached_and_refreshed_after_a_change(world, monkeypatch):
    pbx, code = world["pbx"], world["acme"]["code"]
    shows = []

    def cli(cmd, timeout=8):
        if cmd == "database show":
            shows.append(1)
            return ""
        return "Updated database successfully" if cmd.startswith("database put") else ""
    monkeypatch.setattr(pbx, "asterisk_cli", cli)
    admin = world["client"]("admin")
    admin.get("/api/forwarding")
    admin.get("/api/forwarding")
    assert len(shows) == 1
    assert admin.put("/api/extensions/201/forwarding", json={"dnd": True}).status_code == 200
    admin.get("/api/forwarding")
    assert len(shows) == 2
