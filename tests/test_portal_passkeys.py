"""Passkeys in the portal, against the real WebAuthn library, with a
software authenticator (a P-256 key, "none" attestation)."""
import base64
import hashlib
import json
import os
import struct

import cbor2
import pyotp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from conftest import PORTAL_ORIGIN
from test_portal_auth import PW, clock, code_at, login, make_user  # noqa: F401

RP_ID = "testserver"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Authenticator:
    def __init__(self, rp_id=RP_ID, uv=True, origin=PORTAL_ORIGIN):
        self.rp_id, self.uv, self.origin = rp_id, uv, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.user_handle = None

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _head(self, at=False) -> bytes:
        flags = 0x01 | (0x04 if self.uv else 0) | (0x40 if at else 0)
        return hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)

    def _client(self, kind, challenge) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def create(self, options) -> dict:
        self.user_handle = options["user"]["id"]
        cdj = self._client("webauthn.create", options["challenge"])
        auth_data = self._head(at=True) + b"\0" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att), "transports": ["internal", "hybrid"]},
                "clientExtensionResults": {}}

    def get(self, options, handle=None) -> dict:
        cdj = self._client("webauthn.get", options["challenge"])
        auth_data = self._head()
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth_data), "signature": b64u(sig),
                             "userHandle": handle if handle is not None else self.user_handle},
                "clientExtensionResults": {}}


def browser(portal_app):
    return TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})


def signed_in(portal_app, clock, email="user@acme.example", role="user"):
    secret = pyotp.random_base32()
    make_user(portal_app, email=email, role=role, totp=secret)
    c = browser(portal_app)
    login(c, email)
    assert c.post("/api/login/code", json={"code": code_at(secret, clock.t)}).status_code == 200
    return c, secret


def add_passkey(c, a=None, name="iPhone"):
    a = a or Authenticator()
    start = c.post("/api/passkeys/register/start", json={"password": PW})
    assert start.status_code == 200, start.text
    r = c.post("/api/passkeys/register/finish", json={"challenge_id": start.json()["challenge_id"],
                                                       "credential": a.create(start.json()["options"]), "name": name})
    return a, r


def passkey_login(c, a, **kw):
    start = c.post("/api/passkey/login/start").json()
    return c.post("/api/passkey/login/finish", json={"challenge_id": start["challenge_id"],
                                                      "credential": a.get(start["options"], **kw)})


def test_add_and_sign_in(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    a, r = add_passkey(c)
    assert r.status_code == 200, r.text
    keys = c.get("/api/passkeys").json()["passkeys"]
    assert [k["name"] for k in keys] == ["iPhone"]
    other = browser(portal_app)
    r = passkey_login(other, a)
    assert r.status_code == 200 and r.json()["state"] == "full" and r.json()["me"]["email"] == "user@acme.example"
    assert other.get("/api/me").status_code == 200
    assert c.get("/api/passkeys").json()["passkeys"][0]["last_used"]


def test_options_ask_for_verification_and_a_resident_key(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    o = c.post("/api/passkeys/register/start", json={"password": PW}).json()["options"]
    assert o["rp"]["id"] == RP_ID and o["authenticatorSelection"]["residentKey"] == "required"
    assert o["authenticatorSelection"]["userVerification"] == "required"
    assert o["user"]["name"] == "user@acme.example" and o["user"]["id"] not in ("user@acme.example", "1")
    lo = c.post("/api/passkey/login/start").json()["options"]
    assert lo["userVerification"] == "required" and lo["rpId"] == RP_ID


def test_adding_needs_the_password(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    assert c.post("/api/passkeys/register/start", json={"password": "not it at all"}).status_code == 400
    assert browser(portal_app).post("/api/passkeys/register/start", json={"password": PW}).status_code == 401


def test_already_added_is_excluded(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    o = c.post("/api/passkeys/register/start", json={"password": PW}).json()["options"]
    assert [x["id"] for x in o["excludeCredentials"]] == [b64u(a.cred_id)]


def test_challenge_once_and_bound_to_the_account(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    start = c.post("/api/passkeys/register/start", json={"password": PW}).json()
    a = Authenticator()
    cred = a.create(start["options"])
    # another account can't finish it
    c2, _ = signed_in(portal_app, clock, email="other@acme.example")
    assert c2.post("/api/passkeys/register/finish", json={"challenge_id": start["challenge_id"], "credential": cred}).status_code == 400
    # and it's gone now, even for its own account
    assert c.post("/api/passkeys/register/finish", json={"challenge_id": start["challenge_id"], "credential": cred}).status_code == 400
    # sign-in challenges too
    a, _ = add_passkey(c)
    start = c.post("/api/passkey/login/start").json()
    cred = a.get(start["options"])
    b = browser(portal_app)
    assert b.post("/api/passkey/login/finish", json={"challenge_id": start["challenge_id"], "credential": cred}).status_code == 200
    assert b.post("/api/passkey/login/finish", json={"challenge_id": start["challenge_id"], "credential": cred}).status_code == 401


def test_challenge_expires(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    start = c.post("/api/passkey/login/start").json()
    clock.move(minutes=6)
    r = browser(portal_app).post("/api/passkey/login/finish", json={"challenge_id": start["challenge_id"],
                                                                     "credential": a.get(start["options"])})
    assert r.status_code == 401


def test_refused_without_verification_wrong_origin_or_rp(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    a.uv = False
    assert passkey_login(browser(portal_app), a).status_code == 401
    a.uv, a.origin = True, "https://evil.example"
    assert passkey_login(browser(portal_app), a).status_code == 401
    a.origin, a.rp_id = PORTAL_ORIGIN, "evil.example"
    assert passkey_login(browser(portal_app), a).status_code == 401
    # registering without verification
    _, r = add_passkey(c, Authenticator(uv=False))
    assert r.status_code == 400


def test_unknown_passkey_and_wrong_handle(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    assert passkey_login(browser(portal_app), Authenticator()).status_code == 401
    a, _ = add_passkey(c)
    assert passkey_login(browser(portal_app), a, handle=b64u(b"someone else")).status_code == 401


def test_disabled_or_locked_account(portal, clock):
    portal_app, _ = portal
    from gwportal import db
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    with db.connect() as conn:
        conn.execute("UPDATE users SET disabled = 1")
        conn.commit()
    assert passkey_login(browser(portal_app), a).status_code == 401
    with db.connect() as conn:
        conn.execute("UPDATE users SET disabled = 0, locked_until = '2999-01-01'")
        conn.commit()
    assert passkey_login(browser(portal_app), a).status_code == 401


def test_failures_count_towards_the_address_limit(portal, clock):
    portal_app, _ = portal
    b = browser(portal_app)
    for _ in range(20):
        passkey_login(b, Authenticator())
    assert b.post("/api/passkey/login/start").status_code == 429


def test_synced_passkey_count_stays_zero(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    for _ in range(3):  # iCloud and Google passkeys always report 0
        assert passkey_login(browser(portal_app), a).status_code == 200


def test_count_going_backwards_is_refused(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    a.count = 5
    assert passkey_login(browser(portal_app), a).status_code == 200
    a.count = 3  # a cloned key
    assert passkey_login(browser(portal_app), a).status_code == 401


def test_admin_without_codes_must_set_them_up(portal, clock):
    portal_app, _ = portal
    from gwportal import db
    c, _ = signed_in(portal_app, clock, email="admin@acme.example", role="admin")
    a, _ = add_passkey(c)
    assert passkey_login(browser(portal_app), a).json()["state"] == "full"
    with db.connect() as conn:  # the operator reset them
        conn.execute("UPDATE users SET totp_secret = NULL")
        conn.commit()
    assert passkey_login(browser(portal_app), a).json()["state"] == "enrol"


def test_remove(portal, clock):
    portal_app, _ = portal
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    pid = c.get("/api/passkeys").json()["passkeys"][0]["id"]
    c2, _ = signed_in(portal_app, clock, email="other@acme.example")
    assert c2.delete(f"/api/passkeys/{pid}").status_code == 404  # not theirs
    assert c.delete(f"/api/passkeys/{pid}").status_code == 200
    assert passkey_login(browser(portal_app), a).status_code == 401


def test_operator_reset_removes_passkeys(portal, clock, tmp_path):
    portal_app, _ = portal
    from gwportal import cli
    c, _ = signed_in(portal_app, clock)
    a, _ = add_passkey(c)
    assert cli.main(["--env", str(tmp_path / "no.env"), "reset-2fa", "--email", "user@acme.example"]) == 0
    assert passkey_login(browser(portal_app), a).status_code == 401
