"""The portal's sign-in: passwords, lockouts, two-factor codes, sessions,
invitations and resets (portal/gwportal/auth.py and app.py)."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from fastapi.testclient import TestClient

from conftest import PORTAL_ORIGIN

PW = "correct horse battery"
T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def move(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock(portal, monkeypatch):
    c = Clock()
    from gwportal import clock as clock_module
    monkeypatch.setattr(clock_module, "now", c)
    return c


def make_user(portal_app, email="user@acme.example", role="user", password=PW, totp=None, disabled=False):
    from gwportal import auth, db
    with db.connect() as conn:
        u = portal_app.create_user(conn, email, "1002", role, name="Test")
        if password:
            conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (auth.hash_password(password), u["id"]))
        if totp:
            conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (auth._fernet().encrypt(totp.encode()).decode(),
                                                                           u["id"]))
        if disabled:
            conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (u["id"],))
        return dict(conn.execute("SELECT * FROM users WHERE id = ?", (u["id"],)).fetchone())


def login(c, email="user@acme.example", password=PW, ip=None):
    return c.post("/api/login", json={"email": email, "password": password},
                  headers={"X-Real-IP": ip} if ip else {})


def code_at(secret, t):
    return pyotp.TOTP(secret).at(int(t.timestamp()))


def cookie(c):
    return c.cookies.get("__Host-gwportal")


# -- passwords and lockouts ----------------------------------------------------------------

def test_sign_in_and_the_cookie(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    r = login(c)
    assert r.status_code == 200 and r.json()["state"] == "full"
    set_cookie = r.headers["set-cookie"]
    for part in ("__Host-gwportal=", "HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert part.lower() in set_cookie.lower(), part
    assert "domain" not in set_cookie.lower()
    assert c.get("/api/me").json()["email"] == "user@acme.example"


def test_one_message_for_every_failure(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    make_user(portal_app, "off@acme.example", disabled=True)
    make_user(portal_app, "new@acme.example", password=None)
    answers = {(r.status_code, r.json()["detail"]) for r in (
        login(c, password="wrong password!"), login(c, "nobody@acme.example"),
        login(c, "off@acme.example"), login(c, "new@acme.example"))}
    assert len(answers) == 1 and answers.pop()[0] == 401


def test_email_case_and_spaces(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    assert login(c, "  User@ACME.example ").json()["state"] == "full"


def test_account_lockout(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    for _ in range(5):
        assert login(c, password="wrong password!").status_code == 401
    assert login(c).status_code == 401  # locked, even with the right password
    clock.move(minutes=16)
    assert login(c).status_code == 200


def test_address_limit(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    for i in range(20):
        login(c, f"nobody{i}@acme.example", ip="203.0.113.5")
    assert login(c, ip="203.0.113.5").status_code == 429
    assert login(c, ip="203.0.113.6").status_code == 200
    clock.move(minutes=16)
    assert login(c, ip="203.0.113.5").status_code == 200


def test_changes_only_from_the_portal(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    bare = TestClient(portal_app.app, base_url=PORTAL_ORIGIN)
    assert bare.post("/api/login", json={"email": "user@acme.example", "password": PW}).status_code == 403
    assert bare.post("/api/login", json={"email": "user@acme.example", "password": PW},
                     headers={"Origin": "https://evil.example"}).status_code == 403


def test_no_api_description(portal):
    portal_app, c = portal
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert c.get(path).status_code == 404


# -- sessions ---------------------------------------------------------------------------------

def test_idle_and_absolute_limits(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    login(c)
    clock.move(minutes=119)
    assert c.get("/api/me").status_code == 200
    clock.move(minutes=121)  # idle over 2 hours
    assert c.get("/api/me").status_code == 401
    login(c)
    for _ in range(12):
        clock.move(minutes=59)
        assert c.get("/api/me").status_code == 200  # 11 h 48 min, kept busy
    clock.move(minutes=13)
    assert c.get("/api/me").status_code == 401  # past 12 hours, however busy


def test_logout(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    login(c)
    token = cookie(c)
    assert c.post("/api/logout").status_code == 200
    c.cookies.set("__Host-gwportal", token)
    assert c.get("/api/me").status_code == 401  # the token itself is gone


# -- two-factor codes ----------------------------------------------------------------------------

def test_admin_must_set_up_codes_first(portal, clock):
    portal_app, c = portal
    make_user(portal_app, "admin@acme.example", role="admin")
    assert login(c, "admin@acme.example").json()["state"] == "enrol"
    first = cookie(c)
    assert c.get("/api/me").status_code == 403
    assert c.post("/api/password/change", json={"current": PW, "new": "another long password"}).status_code == 403
    uri = c.post("/api/2fa/start").json()["uri"]
    secret = parse_qs(urlsplit(uri).query)["secret"][0]
    assert c.post("/api/2fa/finish", json={"code": "000000"}).status_code == 400
    r = c.post("/api/2fa/finish", json={"code": code_at(secret, clock.t)})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
    assert cookie(c) != first  # a new token once fully signed in
    assert c.get("/api/me").json()["two_factor"] is True
    c.cookies.set("__Host-gwportal", first)
    assert c.get("/api/me").status_code == 401


def test_code_after_password(portal, clock):
    portal_app, c = portal
    secret = pyotp.random_base32()
    make_user(portal_app, totp=secret)
    assert login(c).json()["state"] == "pending_mfa"
    pending = cookie(c)
    assert c.get("/api/me").status_code == 403
    r = c.post("/api/login/code", json={"code": code_at(secret, clock.t)})
    assert r.status_code == 200 and cookie(c) != pending
    assert c.get("/api/me").status_code == 200


def test_a_code_works_once(portal, clock):
    portal_app, c = portal
    secret = pyotp.random_base32()
    make_user(portal_app, totp=secret)
    code = code_at(secret, clock.t)
    login(c)
    assert c.post("/api/login/code", json={"code": code}).status_code == 200
    c.post("/api/logout")
    login(c)
    assert c.post("/api/login/code", json={"code": code}).status_code == 401  # same step again
    clock.move(seconds=30)
    assert c.post("/api/login/code", json={"code": code_at(secret, clock.t)}).status_code == 200


def test_too_many_wrong_codes(portal, clock):
    portal_app, c = portal
    secret = pyotp.random_base32()
    make_user(portal_app, totp=secret)
    login(c)
    for i in range(4):
        assert c.post("/api/login/code", json={"code": f"00000{i}"}).json()["detail"] == "That code isn't right."
    assert c.post("/api/login/code", json={"code": "000009"}).json()["detail"].startswith("Too many")
    assert c.post("/api/login/code", json={"code": code_at(secret, clock.t)}).status_code == 401  # session gone


def test_recovery_code_once(portal, clock):
    portal_app, c = portal
    make_user(portal_app, "admin@acme.example", role="admin")
    login(c, "admin@acme.example")
    secret = parse_qs(urlsplit(c.post("/api/2fa/start").json()["uri"]).query)["secret"][0]
    codes = c.post("/api/2fa/finish", json={"code": code_at(secret, clock.t)}).json()["recovery_codes"]
    c.post("/api/logout")
    login(c, "admin@acme.example")
    assert c.post("/api/login/code", json={"recovery_code": codes[0]}).status_code == 200
    c.post("/api/logout")
    login(c, "admin@acme.example")
    assert c.post("/api/login/code", json={"recovery_code": codes[0]}).status_code == 401


def test_admins_keep_codes_users_may_turn_them_off(portal, clock):
    portal_app, c = portal
    secret = pyotp.random_base32()
    make_user(portal_app, totp=secret)
    login(c)
    c.post("/api/login/code", json={"code": code_at(secret, clock.t)})
    clock.move(seconds=30)
    assert c.post("/api/2fa/off", json={"password": "wrong", "code": code_at(secret, clock.t)}).status_code == 400
    assert c.post("/api/2fa/off", json={"password": PW, "code": code_at(secret, clock.t)}).status_code == 200
    a = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    make_user(portal_app, "admin@acme.example", role="admin", totp=secret)
    login(a, "admin@acme.example")
    clock.move(seconds=30)
    a.post("/api/login/code", json={"code": code_at(secret, clock.t)})
    assert a.post("/api/2fa/off", json={"password": PW, "code": "123456"}).status_code == 403


def test_codes_stored_encrypted(portal, clock):
    portal_app, c = portal
    make_user(portal_app, "admin@acme.example", role="admin")
    login(c, "admin@acme.example")
    secret = parse_qs(urlsplit(c.post("/api/2fa/start").json()["uri"]).query)["secret"][0]
    c.post("/api/2fa/finish", json={"code": code_at(secret, clock.t)})
    raw = open(portal_app.config.DB_PATH, "rb").read()
    assert secret.encode() not in raw


# -- forgotten passwords -----------------------------------------------------------------------------

@pytest.fixture
def mails(portal, monkeypatch):
    from gwportal import mail
    sent = []
    monkeypatch.setattr(mail, "send", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr(mail, "ready", lambda: True)
    return sent


def link_token(body, page):
    import re
    m = re.search(rf'href="{re.escape(PORTAL_ORIGIN)}/{page}#([A-Za-z0-9_-]+)"', body)
    return m.group(1) if m else None


def test_forgot_same_reply_and_link_from_origin(portal, clock, mails):
    portal_app, c = portal
    make_user(portal_app)
    a = c.post("/api/password/forgot", json={"email": "user@acme.example"}, headers={"Host": "evil.example"})
    b = c.post("/api/password/forgot", json={"email": "nobody@acme.example"})
    assert a.json() == b.json() and a.status_code == b.status_code == 200
    assert len(mails) == 1 and "evil.example" not in mails[0][2]
    assert link_token(mails[0][2], "reset")  # the token is in the fragment, on the portal's own address


def test_reset(portal, clock, mails):
    portal_app, c = portal
    make_user(portal_app)
    other = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    login(other)
    for _ in range(5):
        login(c, password="wrong password!")  # locked
    c.post("/api/password/forgot", json={"email": "user@acme.example"})
    token = link_token(mails[0][2], "reset")
    assert c.post("/api/password/reset", json={"token": token, "password": "short"}).status_code == 400
    assert c.post("/api/password/reset", json={"token": token, "password": "a brand new password"}).status_code == 200
    assert c.post("/api/password/reset", json={"token": token, "password": "another new password"}).status_code == 400
    assert other.get("/api/me").status_code == 401  # signed out everywhere
    assert login(c, password="a brand new password").status_code == 200  # and the lockout cleared


def test_reset_expires(portal, clock, mails):
    portal_app, c = portal
    make_user(portal_app)
    c.post("/api/password/forgot", json={"email": "user@acme.example"})
    clock.move(minutes=61)
    r = c.post("/api/password/reset", json={"token": link_token(mails[0][2], "reset"), "password": "a brand new password"})
    assert r.status_code == 400


def test_reset_keeps_two_factor(portal, clock, mails):
    portal_app, c = portal
    make_user(portal_app, totp=pyotp.random_base32())
    c.post("/api/password/forgot", json={"email": "user@acme.example"})
    c.post("/api/password/reset", json={"token": link_token(mails[0][2], "reset"), "password": "a brand new password"})
    assert login(c, password="a brand new password").json()["state"] == "pending_mfa"


# -- invitations -------------------------------------------------------------------------------------

def test_invitation(portal, clock):
    portal_app, c = portal
    from gwportal import auth, db
    u = make_user(portal_app, "new@acme.example", password=None)
    with db.connect() as conn:
        token = auth.new_link(conn, u, "invite")
    assert c.post("/api/invite/check", json={"token": token}).json()["email"] == "new@acme.example"
    assert c.post("/api/invite/accept", json={"token": token, "password": "new@acme.example"}).status_code == 400
    assert c.post("/api/invite/accept", json={"token": token, "password": "my own long password"}).status_code == 200
    assert c.post("/api/invite/accept", json={"token": token, "password": "my own long password"}).status_code == 400
    assert login(c, "new@acme.example", "my own long password").status_code == 200
    with db.connect() as conn:
        late = auth.new_link(conn, make_user(portal_app, "late@acme.example", password=None), "invite")
    clock.move(hours=73)
    assert c.post("/api/invite/check", json={"token": late}).status_code == 400


def test_one_email_one_login(portal, clock):
    portal_app, c = portal
    from gwportal import auth, db
    make_user(portal_app)
    with db.connect() as conn, pytest.raises(auth.Refused) as e:
        portal_app.create_user(conn, " USER@acme.example", "1003", "admin")
    assert e.value.status == 409 and "1002" not in str(e.value)


def test_password_change_signs_out_elsewhere(portal, clock):
    portal_app, c = portal
    make_user(portal_app)
    other = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    login(other)
    login(c)
    assert c.post("/api/password/change", json={"current": PW, "new": "a different long one"}).status_code == 200
    assert c.get("/api/me").status_code == 200 and other.get("/api/me").status_code == 401
