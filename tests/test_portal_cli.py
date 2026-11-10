"""The operator's portal command (gwportal.cli), against the real gwphones
app: inviting a tenant's first admin, listing, resetting two-factor
codes, disabling."""
import pyotp
import pytest
from fastapi.testclient import TestClient

from conftest import PORTAL_ORIGIN


@pytest.fixture
def setup(env, portal_env, monkeypatch, tmp_path):
    monkeypatch.setenv("GWPHONES_PORTAL_SECRET", "portal-test-secret")
    import main, pbx
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    assert op.post("/api/pbx/tenants", json={"name": "Acme", "outbound_cid": "015559999"}).status_code == 200
    acme = pbx.get_db().execute("SELECT id, code FROM tenants WHERE name = 'Acme'").fetchone()
    from gwportal import cli, gw, mail
    gw.client = TestClient(main.app, base_url="http://gw")
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr(mail, "ready", lambda: True)
    run = lambda *argv: cli.main(["--env", str(tmp_path / "no.env"), *argv])
    return {"op": op, "pbx": pbx, "acme": acme, "run": run, "sent": sent, "mail": mail}


def users():
    from gwportal import db
    with db.connect() as conn:
        return {u["email"]: dict(u) for u in conn.execute("SELECT * FROM users")}


def test_invite_with_a_printed_link(setup, capsys):
    code = setup["acme"]["code"]
    assert setup["run"]("invite", "--tenant", code, "--email", "Anne@Acme.example", "--name", "Anne", "--print-link") == 0
    out = capsys.readouterr().out
    assert "Acme" in out and f"{PORTAL_ORIGIN}/invite#" in out and not setup["sent"]
    u = users()["anne@acme.example"]
    assert u["role"] == "admin" and u["tenant_code"] == code and u["pw_hash"] is None
    from gwportal import app as portal_app
    c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    token = out.strip().split("#")[-1]
    assert c.post("/api/invite/check", json={"token": token}).json()["email"] == "anne@acme.example"


def test_invite_by_email(setup, capsys):
    assert setup["run"]("invite", "--tenant", setup["acme"]["code"], "--email", "anne@acme.example") == 0
    to, subject, body = setup["sent"][0]
    assert to == "anne@acme.example" and "Acme" in body and f"{PORTAL_ORIGIN}/invite#" in body
    assert "on its way" in capsys.readouterr().out


@pytest.mark.parametrize("tenant,code", [("9999", 1), ("12", 2), ("abcd", 2)])
def test_invite_unknown_or_bad_tenant(setup, tenant, code):
    assert setup["run"]("invite", "--tenant", tenant, "--email", "x@acme.example") == code
    assert users() == {}


def test_invite_suspended_tenant(setup):
    acme = setup["acme"]
    setup["op"].put(f"/api/pbx/tenants/{acme['id']}", json={"name": "Acme", "status": "suspended"})
    assert setup["run"]("invite", "--tenant", acme["code"], "--email", "x@acme.example") == 1
    assert users() == {}


def test_invite_without_email_set_up(setup, monkeypatch):
    monkeypatch.setattr(setup["mail"], "ready", lambda: False)
    assert setup["run"]("invite", "--tenant", setup["acme"]["code"], "--email", "x@acme.example") == 1
    assert users() == {}


def test_invite_email_fails_leaves_nothing(setup, monkeypatch):
    def fail(*a):
        raise setup["mail"].NotSent("Microsoft Graph said 500")
    monkeypatch.setattr(setup["mail"], "send", fail)
    assert setup["run"]("invite", "--tenant", setup["acme"]["code"], "--email", "x@acme.example") == 1
    assert users() == {}


def test_invite_taken_address(setup):
    code = setup["acme"]["code"]
    assert setup["run"]("invite", "--tenant", code, "--email", "x@acme.example", "--print-link") == 0
    assert setup["run"]("invite", "--tenant", "1001", "--email", "X@acme.example", "--print-link") == 1


def test_list(setup, capsys):
    code = setup["acme"]["code"]
    setup["run"]("invite", "--tenant", code, "--email", "x@acme.example", "--print-link")
    capsys.readouterr()
    assert setup["run"]("list", "--tenant", code) == 0
    out = capsys.readouterr().out
    assert "x@acme.example" in out and "admin" in out and "invited" in out


def test_reset_2fa(setup, capsys):
    from gwportal import app as portal_app, auth, db
    secret = pyotp.random_base32()
    with db.connect() as conn:
        u = portal_app.create_user(conn, "anne@acme.example", setup["acme"]["code"], "admin")
        conn.execute("UPDATE users SET pw_hash = ?, totp_secret = ? WHERE id = ?",
                     (auth.hash_password("correct horse battery"), auth._fernet().encrypt(secret.encode()).decode(), u["id"]))
    c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    c.post("/api/login", json={"email": "anne@acme.example", "password": "correct horse battery"})
    c.post("/api/login/code", json={"code": pyotp.TOTP(secret).now()})
    assert c.get("/api/me").status_code == 200
    assert setup["run"]("reset-2fa", "--email", "anne@acme.example") == 0
    assert c.get("/api/me").status_code == 401  # signed out
    r = c.post("/api/login", json={"email": "anne@acme.example", "password": "correct horse battery"})
    assert r.json()["state"] == "enrol"  # an admin sets them up again
    assert setup["run"]("reset-2fa", "--email", "nobody@acme.example") == 1


def test_disable_and_enable(setup):
    code = setup["acme"]["code"]
    setup["run"]("invite", "--tenant", code, "--email", "x@acme.example", "--print-link")
    assert setup["run"]("disable", "--email", "x@acme.example") == 0 and users()["x@acme.example"]["disabled"]
    assert setup["run"]("enable", "--email", "x@acme.example") == 0 and not users()["x@acme.example"]["disabled"]


def test_reads_the_env_file(tmp_path, monkeypatch):
    from conftest import PORTAL
    monkeypatch.syspath_prepend(PORTAL)
    from gwportal import cli
    f = tmp_path / "portal.env"
    f.write_text("# a comment\nPORTAL_TEST_SETTING=from the file\nPORTAL_ALREADY=from the file\n")
    monkeypatch.delenv("PORTAL_TEST_SETTING", raising=False)
    monkeypatch.setenv("PORTAL_ALREADY", "set already")
    cli.load_env(str(f))
    import os
    assert os.environ["PORTAL_TEST_SETTING"] == "from the file" and os.environ["PORTAL_ALREADY"] == "set already"
    monkeypatch.delenv("PORTAL_TEST_SETTING")
