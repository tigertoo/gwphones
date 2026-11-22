"""Microsoft 365 in the portal, against a fake Microsoft: an admin links
the tenant's Microsoft organisation (taken only from a verified sign-in),
people sign in with Microsoft (found only by organisation and object id,
never email; an admin still gives the second factor), and sync keeps
Staff in step with a group, extensions from a custom attribute."""
import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import PORTAL_ORIGIN
from test_portal_phone import PW, ext_row, world  # noqa: F401  (the two-tenant world)

CLIENT = "11111111-2222-3333-4444-555555555555"
ORG = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_ORG = "bbbbbbbb-0000-0000-0000-000000000002"
GROUP = "cccccccc-0000-0000-0000-000000000003"
ADMINS = "dddddddd-0000-0000-0000-000000000004"
SECRET = "client-secret-never-shown"
APP_TOKEN = "graph-app-token-never-shown"


class Answer:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self.data


def id_token(claims: dict) -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2lnbmF0dXJl"


class FakeMicrosoft:
    def __init__(self):
        self.person = None  # who signs in next: {tid, oid, name}, plus overrides
        self.nonce = None
        self.members = {GROUP: [], ADMINS: []}
        self.consented = {ORG}
        self.calls = []

    def __call__(self, method, url, data=None, params=None, headers=None):
        self.calls.append((method, url, data, params, headers))
        if url.endswith("/organizations/oauth2/v2.0/token"):
            assert data["client_secret"] == SECRET and data["code_verifier"] and data["grant_type"] == "authorization_code"
            p = dict(self.person)
            tid = p.pop("tid")
            claims = {"tid": tid, "oid": p.pop("oid"), "name": p.pop("name", "Someone"), "aud": CLIENT,
                      "iss": f"https://login.microsoftonline.com/{tid}/v2.0", "exp": time.time() + 600,
                      "nonce": self.nonce, "email": p.pop("email", "")}
            claims.update(p)
            return Answer({"id_token": id_token(claims), "access_token": "user-token"})
        if "/oauth2/v2.0/token" in url:
            tid = url.split("login.microsoftonline.com/")[1].split("/")[0]
            if tid not in self.consented:
                return Answer({"error": "invalid_client", "error_codes": [7000229]}, 400)
            return Answer({"access_token": APP_TOKEN, "expires_in": 3600})
        assert headers["Authorization"] == f"Bearer {APP_TOKEN}"
        if url.endswith("/groups"):
            name = params["$filter"].split("'")[1]
            found = {"Phone users": GROUP, "Phone admins": ADMINS}.get(name)
            return Answer({"value": [{"id": found, "displayName": name}] if found else []})
        if "/transitiveMembers/" in url:
            gid = url.split("/groups/")[1].split("/")[0]
            return Answer({"value": self.members[gid]})
        raise AssertionError(url)


def member(oid, email, ext="", enabled=True, name=None):
    return {"id": oid, "displayName": name or email.split("@")[0].title(), "mail": email, "userPrincipalName": email,
            "accountEnabled": enabled, "onPremisesExtensionAttributes": {"extensionAttribute1": ext}}


@pytest.fixture
def ms(world, monkeypatch):
    """A fake Microsoft, and a free extension 203 for Acme (ann has 201, bob 202)."""
    pbx = world["pbx"]
    token = pbx._current_tenant.set(world["acme"]["id"])
    try:
        pbx.api_create_extension(pbx.ExtensionCreate(ext="203", name="Free"))
    finally:
        pbx._current_tenant.reset(token)
    from gwportal import microsoft
    fake = FakeMicrosoft()
    monkeypatch.setattr(microsoft, "CLIENT_ID", CLIENT)
    monkeypatch.setattr(microsoft, "CLIENT_SECRET", SECRET)
    monkeypatch.setattr(microsoft, "_send", fake)
    microsoft._tokens.clear()
    return fake


def microsoft_flow(client, fake, path, person, **overrides):
    """Starts a flow, lets "Microsoft" sign `person` in, follows the callback."""
    r = client.post(path)
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["url"]).query)
    assert q["client_id"] == [CLIENT] and q["code_challenge_method"] == ["S256"] and "/organizations/" in r.json()["url"]
    fake.nonce = q["nonce"][0]
    fake.person = {**person, **overrides}
    state = overrides.pop("state", None) or q["state"][0]
    return client.get(f"/api/microsoft/callback?code=abc&state={state}", follow_redirects=False)


def link(world, fake):
    admin = world["client"]("admin")
    r = microsoft_flow(admin, fake, "/api/microsoft/link/start", {"tid": ORG, "oid": "oid-admin"})
    assert r.status_code == 303 and r.headers["location"] == "/?microsoft=linked"
    assert admin.put("/api/microsoft", json={"group": "Phone users", "admin_group": "Phone admins",
                                             "attribute": 1}).status_code == 200
    return admin


def test_link_and_sync_staff_with_extensions(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203"), member("oid-dev", "dev@acme.example", "")]
    preview = admin.post("/api/microsoft/preview").json()
    assert preview["counts"]["added"] == 1 and any("empty" in p["problem"] for p in preview["problems"])
    from gwportal import db
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM users WHERE email = 'cara@acme.example'").fetchone()  # preview writes nothing
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["added"] == 1
    cara = next(u for u in admin.get("/api/users").json()["users"] if u["email"] == "cara@acme.example")
    assert cara["microsoft"] and cara["ext"] == "203" and cara["role"] == "user" and not cara["invited"]
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = 'cara@acme.example'").fetchone()
    assert row["ext_created"] == ext_row(world, world["acme"]["id"], "203")["created_at"]


def test_signin_found_by_object_id_never_email(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    from fastapi.testclient import TestClient
    from gwportal import app as portal_app

    def browser():
        return TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    c = browser()
    r = microsoft_flow(c, ms, "/api/microsoft/signin/start", {"tid": ORG, "oid": "oid-cara"})
    assert r.headers["location"] == "/?microsoft=signed_in"
    assert c.get("/api/session").json()["me"]["email"] == "cara@acme.example"
    # someone else claiming cara's email from another Microsoft organisation gets nothing
    c2 = browser()
    r = microsoft_flow(c2, ms, "/api/microsoft/signin/start", {"tid": OTHER_ORG, "oid": "oid-x",
                                                                "email": "cara@acme.example"})
    assert r.headers["location"] == "/?microsoft=no_account" and c2.get("/api/session").json()["state"] == "none"
    # nor a different account in the same organisation with her email
    c3 = browser()
    r = microsoft_flow(c3, ms, "/api/microsoft/signin/start", {"tid": ORG, "oid": "oid-y", "email": "cara@acme.example",
                                                                "preferred_username": "cara@acme.example"})
    assert r.headers["location"] == "/?microsoft=no_account"


@pytest.mark.parametrize("bad", [{"aud": "someone-else"}, {"iss": "https://evil.example/v2.0"},
                                 {"exp": 1000}, {"nonce": "replayed"}])
def test_bad_tokens_refused(world, ms, bad):
    link(world, ms)
    from fastapi.testclient import TestClient
    from gwportal import app as portal_app
    c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    r = microsoft_flow(c, ms, "/api/microsoft/signin/start", {"tid": ORG, "oid": "oid-admin", **bad})
    assert r.headers["location"] == "/?microsoft=failed" and c.get("/api/session").json()["state"] == "none"


def test_flow_is_single_use_and_bound_to_its_cookie(world, ms):
    link(world, ms)
    from fastapi.testclient import TestClient
    from gwportal import app as portal_app
    c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    r = microsoft_flow(c, ms, "/api/microsoft/signin/start", {"tid": ORG, "oid": "oid-admin"}, state="forged")
    assert r.headers["location"] == "/?microsoft=expired"
    # a callback without the browser's flow cookie (another browser) is refused
    start = c.post("/api/microsoft/signin/start").json()["url"]
    state = parse_qs(urlparse(start).query)["state"][0]
    other = TestClient(portal_app.app, base_url=PORTAL_ORIGIN)
    assert other.get(f"/api/microsoft/callback?code=abc&state={state}",
                     follow_redirects=False).headers["location"] == "/?microsoft=expired"


def test_admin_still_gives_the_second_factor(world, ms):
    link(world, ms)
    from fastapi.testclient import TestClient
    from gwportal import app as portal_app
    c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN, headers={"Origin": PORTAL_ORIGIN})
    microsoft_flow(c, ms, "/api/microsoft/signin/start", {"tid": ORG, "oid": "oid-admin"})
    assert c.get("/api/session").json()["state"] == "pending_mfa"
    assert c.get("/api/users").status_code == 403


def test_one_tenant_per_organisation(world, ms):
    """An organisation already linked to another tenant can't be linked again."""
    from gwportal import db
    with db.connect() as conn:
        conn.execute("INSERT INTO ms_links (tenant_code, ms_tenant) VALUES ('1001', ?)", (ORG,))
    admin = world["client"]("admin")
    r = microsoft_flow(admin, ms, "/api/microsoft/link/start", {"tid": ORG, "oid": "oid-admin"})
    assert r.headers["location"] == "/?microsoft=elsewhere"
    with db.connect() as conn:
        assert conn.execute("SELECT tenant_code FROM ms_links WHERE ms_tenant = ?", (ORG,)).fetchone()[0] == "1001"
        assert conn.execute("SELECT ms_oid FROM users WHERE email = 'admin@acme.example'").fetchone()[0] is None


def test_leavers_disabled_and_back_again(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    ms.members[GROUP] = []
    assert admin.post("/api/microsoft/sync").json()["counts"]["disabled"] == 1
    cara = next(u for u in admin.get("/api/users").json()["users"] if u["email"] == "cara@acme.example")
    assert cara["disabled"]
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "202", enabled=False)]
    assert admin.post("/api/microsoft/sync").json()["counts"]["enabled"] == 0  # account disabled in Microsoft
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    assert admin.post("/api/microsoft/sync").json()["counts"]["enabled"] == 1


def test_problems_reported_not_guessed(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-a", "a@acme.example", "201"), member("oid-b", "b@acme.example", "201"),
                         member("oid-c", "c@acme.example", "299"), member("oid-z", "zoe@main.example", "203")]
    r = admin.post("/api/microsoft/sync").json()
    text = json.dumps(r["problems"])
    assert "share extension 201" in text and "no extension 299" in text and "another organisation" in text
    assert r["counts"]["added"] == 0


def test_local_person_with_same_email_is_linked_not_duplicated(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-ann", "ann@acme.example", "201")]
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["added"] == 1 and r["actions"][0]["do"] == "link"
    assert [u["email"] for u in admin.get("/api/users").json()["users"]].count("ann@acme.example") == 1


def test_last_admin_never_disabled(world, ms):
    admin = link(world, ms)
    ms.members[ADMINS] = [member("oid-admin", "admin@acme.example")]
    ms.members[GROUP] = [member("oid-admin", "admin@acme.example")]
    admin.post("/api/microsoft/sync")  # the admin is now managed by sync
    ms.members[GROUP], ms.members[ADMINS] = [], []
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["disabled"] == 0 and "needs at least one" in json.dumps(r["problems"])
    assert admin.get("/api/users").status_code == 200


def test_not_consented_says_so(world, ms):
    admin = world["client"]("admin")
    ms.consented = set()
    microsoft_flow(admin, ms, "/api/microsoft/link/start", {"tid": ORG, "oid": "oid-admin"})
    r = admin.put("/api/microsoft", json={"group": "Phone users", "attribute": 1})
    assert r.status_code == 400 and "approve" in r.json()["detail"]
    s = admin.get("/api/microsoft").json()
    assert s["consent_url"].startswith(f"https://login.microsoftonline.com/{ORG}/v2.0/adminconsent?")


def test_staff_refused_and_nothing_secret_shown(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    answers = [admin.get("/api/microsoft").text, admin.post("/api/microsoft/sync").text,
               admin.post("/api/microsoft/preview").text]
    for text in answers:
        assert SECRET not in text and APP_TOKEN not in text
    ann = world["client"]("ann")
    for method, path in (("GET", "/api/microsoft"), ("PUT", "/api/microsoft"), ("DELETE", "/api/microsoft"),
                         ("POST", "/api/microsoft/sync"), ("POST", "/api/microsoft/preview"),
                         ("POST", "/api/microsoft/link/start")):
        assert ann.request(method, path, json={}).status_code == 403, path
    from gwportal import db
    with db.connect() as conn:
        log = " ".join(" ".join(str(v) for v in tuple(r)) for r in conn.execute("SELECT * FROM audit"))
    assert SECRET not in log and APP_TOKEN not in log


def test_unlink_disables_people_without_a_password(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    assert admin.delete("/api/microsoft").status_code == 200
    users = {u["email"]: u for u in admin.get("/api/users").json()["users"]}
    assert users["cara@acme.example"]["disabled"] and not users["admin@acme.example"]["disabled"]
    assert admin.get("/api/microsoft").json()["linked"] is False


def test_sync_never_undoes_an_admins_disable(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    cara = next(u for u in admin.get("/api/users").json()["users"] if u["email"] == "cara@acme.example")
    assert admin.put(f"/api/users/{cara['id']}", json={"disabled": True}).status_code == 200
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["enabled"] == 0 and "disabled by an admin" in json.dumps(r["problems"])
    assert next(u for u in admin.get("/api/users").json()["users"] if u["id"] == cara["id"])["disabled"]
    # role and extension come from Microsoft 365
    r = admin.put(f"/api/users/{cara['id']}", json={"role": "admin"})
    assert r.status_code == 400 and "Microsoft 365" in r.json()["detail"]


def test_disabled_local_person_not_linked(world, ms):
    admin = link(world, ms)
    from gwportal import db
    with db.connect() as conn:
        bob = conn.execute("SELECT id FROM users WHERE email = 'bob@acme.example'").fetchone()["id"]
    admin.put(f"/api/users/{bob}", json={"disabled": True})
    ms.members[GROUP] = [member("oid-bob", "bob@acme.example", "203")]
    r = admin.post("/api/microsoft/sync").json()
    assert r["actions"] == [] and "disabled by an admin" in json.dumps(r["problems"])


def test_guests_skipped(world, ms):
    admin = link(world, ms)
    g = member("oid-guest", "guest@elsewhere.example", "203")
    g["userType"] = "Guest"
    ms.members[GROUP] = [g]
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["added"] == 0 and "guest" in json.dumps(r["problems"])


def test_approval_link_before_connecting(world, ms):
    s = world["client"]("admin").get("/api/microsoft").json()
    assert s["linked"] is False
    assert s["consent_url"].startswith("https://login.microsoftonline.com/organizations/v2.0/adminconsent?")


def test_sync_wont_put_two_people_on_one_extension(world, ms):
    admin = link(world, ms)
    # ann (invited by email) is on 201
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "201")]
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["added"] == 0 and "extension 201 is already" in json.dumps(r["problems"])


def test_still_in_the_group_with_a_problem_isnt_disabled(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "299")]  # a typo in Microsoft 365
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["disabled"] == 0 and "no extension 299" in json.dumps(r["problems"])
    cara = next(u for u in admin.get("/api/users").json()["users"] if u["email"] == "cara@acme.example")
    assert not cara["disabled"] and cara["ext"] == "203"


def ext_of(world, ext):
    return ext_row(world, world["acme"]["id"], ext)


def with_phone(m, phone):
    m["businessPhones"] = [phone]
    return m


def acme_number(world, did):
    pbx = world["pbx"]
    token = pbx._current_tenant.set(world["acme"]["id"])
    try:
        pbx.api_set_inbound_route(pbx.InboundRouteRequest(did=did, target="201"))
    finally:
        pbx._current_tenant.reset(token)


def test_extension_details_from_microsoft_365(world, ms):
    admin = link(world, ms)
    acme_number(world, "35315557777")
    ms.members[GROUP] = [with_phone(member("oid-cara", "cara@acme.example", "203", name="Cara Ó Murphy"), "01 555 7777")]
    preview = admin.post("/api/microsoft/preview").json()
    assert {x["do"] for x in preview["extensions"]} == {"rename", "voicemail_email", "caller_id"}
    assert ext_of(world, "203")["name"] == "Free"  # preview writes nothing
    admin.post("/api/microsoft/sync")
    e = ext_of(world, "203")
    assert e["name"] == "Cara O Murphy" and e["vm_email"] == "cara@acme.example" and e["outbound_cid"] == "+35315557777"
    # nothing to do the second time
    assert admin.post("/api/microsoft/preview").json()["extensions"] == []


def test_caller_id_only_a_tenant_number_and_voicemail_email_not_overwritten(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [with_phone(member("oid-cara", "cara@acme.example", "203"), "087 123 4567")]  # her mobile
    admin.post("/api/microsoft/sync")
    assert ext_of(world, "203")["outbound_cid"] == ""
    admin.put("/api/extensions/203/voicemail-email", json={"email": "office@acme.example"})
    ms.members[GROUP] = [with_phone(member("oid-cara", "cara@acme.example", "203"), "01 555 9999")]  # the main number
    r = admin.post("/api/microsoft/sync").json()
    assert all(x["do"] != "voicemail_email" for x in r["extensions"])
    assert ext_of(world, "203")["vm_email"] == "office@acme.example" and ext_of(world, "203")["outbound_cid"] == ""


def test_missing_extension_created_when_chosen(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-dan", "dan@acme.example", "205", name="Dan"),
                         member("oid-eve", "eve@acme.example", "999")]
    r = admin.post("/api/microsoft/sync").json()
    assert "no extension 205" in json.dumps(r["problems"])  # off by default
    assert admin.put("/api/microsoft", json={"group": "Phone users", "admin_group": "Phone admins", "attribute": 1,
                                             "create_exts": True}).status_code == 200
    preview = admin.post("/api/microsoft/preview").json()
    assert any(x["do"] == "create" and x["ext"] == "205" and x["name"] == "Dan" for x in preview["extensions"])
    assert "no extension 999" in json.dumps(preview["problems"])  # not an extension number: never made
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["added"] == 1
    e = ext_of(world, "205")
    assert e["name"] == "Dan" and e["enabled"]
    from gwportal import db
    with db.connect() as conn:
        dan = conn.execute("SELECT * FROM users WHERE email = 'dan@acme.example'").fetchone()
    assert dan["ext"] == "205" and dan["ext_created"] == e["created_at"]


def test_leavers_extension_switched_off_and_back_on(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    ms.members[GROUP] = []
    r = admin.post("/api/microsoft/sync").json()
    assert [x["do"] for x in r["extensions"]] == ["switch_off"] and not ext_of(world, "203")["enabled"]
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    assert ext_of(world, "203")["enabled"]


def test_an_admins_switched_off_extension_stays_off(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203", name="Cara")]
    admin.post("/api/microsoft/sync")
    assert admin.put("/api/extensions/203", json={"name": "Cara", "enabled": False}).status_code == 200
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203", name="Cara Murphy")]
    admin.post("/api/microsoft/sync")
    e = ext_of(world, "203")
    assert e["name"] == "Cara Murphy" and not e["enabled"]  # renamed, still off


def test_emptied_attribute_switches_login_and_extension_off(world, ms):
    admin = link(world, ms)
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "")]  # number removed in Exchange
    r = admin.post("/api/microsoft/sync").json()
    assert r["counts"]["disabled"] == 1 and [x["do"] for x in r["extensions"]] == ["switch_off"]
    cara = next(u for u in admin.get("/api/users").json()["users"] if u["email"] == "cara@acme.example")
    assert cara["disabled"] and not ext_of(world, "203")["enabled"]
    # the number put back: both on again
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    cara = next(u for u in admin.get("/api/users").json()["users"] if u["email"] == "cara@acme.example")
    assert not cara["disabled"] and ext_of(world, "203")["enabled"]


def test_moved_to_another_number_frees_the_old_one(world, ms):
    admin = link(world, ms)
    admin.put("/api/microsoft", json={"group": "Phone users", "admin_group": "Phone admins", "attribute": 1,
                                      "create_exts": True})
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "203")]
    admin.post("/api/microsoft/sync")
    ms.members[GROUP] = [member("oid-cara", "cara@acme.example", "205")]
    r = admin.post("/api/microsoft/sync").json()
    assert {x["do"] for x in r["extensions"]} >= {"create", "switch_off"}
    assert ext_of(world, "205")["enabled"] and not ext_of(world, "203")["enabled"]
    cara = next(u for u in admin.get("/api/users").json()["users"] if u["email"] == "cara@acme.example")
    assert cara["ext"] == "205" and not cara["disabled"]


def sites_for(world, *specs):
    pbx = world["pbx"]
    token = pbx._current_tenant.set(world["acme"]["id"])
    try:
        ids = {}
        for name, lo, hi in specs:
            r = pbx.api_create_site(pbx.SiteRequest(name=name, ext_min=lo, ext_max=hi))
            ids[name] = r["id"]
        return ids
    finally:
        pbx._current_tenant.reset(token)


def with_office(m, office):
    m["officeLocation"] = office
    return m


def test_site_from_office_location(world, ms):
    admin = link(world, ms)
    ids = sites_for(world, ("Dublin", 201, 209), ("Cork", 210, 219))
    assert admin.put("/api/microsoft", json={"group": "Phone users", "admin_group": "Phone admins", "attribute": 1,
                                             "create_exts": True, "sites_from_office": True}).status_code == 200
    ms.members[GROUP] = [with_office(member("oid-cara", "cara@acme.example", "203"), " dublin "),
                         with_office(member("oid-dan", "dan@acme.example", "211"), "Cork"),
                         with_office(member("oid-eve", "eve@acme.example", "204"), "Galway"),
                         with_office(member("oid-fay", "fay@acme.example", "205"), "Cork")]
    preview = admin.post("/api/microsoft/preview").json()
    text = json.dumps(preview)
    assert {"do": "site", "ext": "203", "site_id": ids["Dublin"], "site": "Dublin", "who": "cara@acme.example"} \
        in preview["extensions"]
    assert any(x["do"] == "create" and x["ext"] == "211" and x["site"] == "Cork" for x in preview["extensions"])
    assert "Galway" in text and "isn't one of your sites" in text and "205 is outside Cork's numbers" in text
    admin.post("/api/microsoft/sync")
    assert ext_of(world, "203")["site_id"] == ids["Dublin"] and ext_of(world, "211")["site_id"] == ids["Cork"]
    assert ext_of(world, "204")["site_id"] is None and ext_of(world, "205")["site_id"] is None
    # nothing to do the second time (apart from the same problems)
    assert all(x["do"] != "site" for x in admin.post("/api/microsoft/preview").json()["extensions"])


def test_office_ignored_unless_chosen(world, ms):
    admin = link(world, ms)
    sites_for(world, ("Dublin", 201, 209))
    ms.members[GROUP] = [with_office(member("oid-cara", "cara@acme.example", "203"), "Dublin")]
    r = admin.post("/api/microsoft/preview").json()
    assert all(x["do"] != "site" for x in r["extensions"]) and "Office" not in json.dumps(r["problems"])


def test_sync_fills_the_company_directory(world, ms):
    admin = link(world, ms)
    acme_number(world, "35315557777")
    m = with_phone(member("oid-cara", "cara@acme.example", "203", name="Cara Murphy"), "01 555 7777")
    m["mobilePhone"], m["department"] = "+353 87 123 4567", "Accounts"
    ms.members[GROUP] = [m]
    preview = admin.post("/api/microsoft/preview").json()
    assert preview["directory"] == []  # nobody synced yet: nothing changes before Sync now
    r = admin.post("/api/microsoft/sync").json()
    assert r["directory"] == [{"ext": "203", "mobile": "+353871234567", "ddi": "+35315557777", "department": "Accounts"}]
    row = world["pbx"].get_db().execute("SELECT mobile, ddi, department FROM directory_extras WHERE tenant_id = ? "
                                        "AND ext = '203'", (world["acme"]["id"],)).fetchone()
    assert tuple(row) == ("0871234567", "015557777", "Accounts")
    import os
    dial = open(os.path.join(world["pbx"].GENERATED_DIR, "extensions_portal.conf")).read()
    assert "exten => 0871234567,1,Set(CALLERID(name)=Cara Murphy (mobile))" in dial
