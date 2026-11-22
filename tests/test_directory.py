"""The company directory: Microsoft 365's details for each extension (the
portal's sync), on the desk phones as one phone book per site (their own
first, with their own credentials), and as names for incoming calls from
the tenant's own people's numbers, within the tenant only."""
import os

from fastapi.testclient import TestClient

from test_four_digit import setup  # noqa: F401


def tenant_with_people(env, monkeypatch):
    op, pbx, tid, code = setup(env, monkeypatch)
    o = op.post("/api/pbx/sites", json={"name": "Ogonnolloe", "ext_min": 2000, "ext_max": 2999}).json()["id"]
    k = op.post("/api/pbx/sites", json={"name": "Killaloe", "ext_min": 3000, "ext_max": 3999}).json()["id"]
    for ext, name, site in (("2000", "Cara Murphy", o), ("2001", "Dan Ryan", o), ("3000", "Sean O Brien", k)):
        assert op.post("/api/pbx/extensions", json={"ext": ext, "name": name, "site_id": site}).status_code == 200
    r = op.put("/api/pbx/directory", json={"entries": [
        {"ext": "2000", "mobile": "+353 87 123 4567", "ddi": "061 555 200", "department": "Accounts"},
        {"ext": "3000", "mobile": "0861112222", "department": "Sales, Ireland"},
        {"ext": "9999", "mobile": "0860000000"}]})  # not an extension: ignored
    assert r.status_code == 200 and r.json()["entries"] == 2
    return op, pbx, tid, code, o, k


def test_phone_books_per_site_own_first(env, monkeypatch):
    op, pbx, tid, code, o, k = tenant_with_people(env, monkeypatch)
    import directory
    with pbx.get_db() as conn:
        books = directory.books(conn, tid, k)
        assert [b["name"] for b in books] == ["Killaloe", "Ogonnolloe"]
        xml = directory.book_xml(conn, tid, books[1])
    assert "<Name>Cara Murphy - Accounts</Name>" in xml and '<Telephone label="Mobile">0871234567</Telephone>' in xml
    assert '<Telephone label="Direct">061555200</Telephone>' in xml and "Dan Ryan" in xml and "Brien" not in xml
    assert '<Telephone label="Extension">2000</Telephone>' in xml


def test_desk_phone_fetches_its_books_with_its_own_credentials(env, monkeypatch):
    op, pbx, tid, code, o, k = tenant_with_people(env, monkeypatch)
    mac = "805ec0000aa1"
    assert op.post("/api/pbx/desk-phones", json={"mac": mac, "ext": "2001", "model": "T41P"}).status_code == 200
    pw = pbx.get_db().execute("SELECT prov_pass FROM desk_phones WHERE mac = ?", (mac,)).fetchone()[0]
    import main
    phone = TestClient(main.app)
    cfg = phone.get(f"/prov/yealink/{mac}.cfg", auth=(mac, pw)).text
    assert "remote_phonebook.data.1.name = Ogonnolloe" in cfg and "remote_phonebook.data.2.name = Killaloe" in cfg
    assert f"remote_phonebook.data.1.url = https://{mac}:{pw}@pbx.example/prov/yealink/phonebook/1.xml" in cfg
    assert "remote_phonebook.data.3.url = \n" in cfg and "features.remote_phonebook.flash_time = 3600" in cfg
    r = phone.get("/prov/yealink/phonebook/1.xml", auth=(mac, pw))
    assert r.status_code == 200 and "Cara Murphy - Accounts" in r.text and r.headers["cache-control"] == "no-store"
    assert "Brien" in phone.get("/prov/yealink/phonebook/2.xml", auth=(mac, pw)).text
    assert phone.get("/prov/yealink/phonebook/1.xml", auth=(mac, "wrong")).status_code == 401
    assert phone.get("/prov/yealink/phonebook/1.xml").status_code == 401
    assert phone.get("/prov/yealink/phonebook/3.xml", auth=(mac, pw)).status_code == 404
    # the checking copy hides the password in the books' links
    shown = op.get(f"/api/pbx/desk-phones/{mac}/config.cfg").text
    assert pw not in shown and "remote_phonebook.data.1.url = https://********@" in shown


def test_names_for_incoming_calls_within_the_tenant(env, monkeypatch):
    op, pbx, tid, code, o, k = tenant_with_people(env, monkeypatch)
    dial = open(os.path.join(pbx.GENERATED_DIR, "extensions_portal.conf")).read()
    cnam = dial.split(f"[t{code}-cnam]")[1].split("\n[")[0]
    for form in ("353871234567", "0871234567", "+353871234567", "00353871234567"):
        assert f"exten => {form},1,Set(CALLERID(name)=Cara Murphy (mobile))" in cnam
    assert "Set(CALLERID(name)=Cara Murphy (direct))" in cnam and "Sean O Brien (mobile)" in cnam
    # only this tenant's: the main tenant's lookup knows none of them
    main_cnam = dial.split("[portal-cnam]")[1].split("\n[")[0]
    assert "Cara" not in main_cnam
    # and its incoming calls look it up, only if the number is there
    op.cookies.set("pbx_tenant", str(tid))
    token = pbx._current_tenant.set(tid)
    try:
        pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35361555000", target="2000"))
    finally:
        pbx._current_tenant.reset(token)
    dial = open(os.path.join(pbx.GENERATED_DIR, "extensions_portal.conf")).read()
    route = dial.split("exten => 35361555000,1,")[1].split("\nexten => ")[0]
    assert f"GosubIf($[${{DIALPLAN_EXISTS(t{code}-cnam,${{CALLERID(num)}},1)}}]?t{code}-cnam,${{CALLERID(num)}},1)" in route


def test_bad_numbers_and_unchanged_directory(env, monkeypatch):
    op, pbx, tid, code, o, k = tenant_with_people(env, monkeypatch)
    assert op.put("/api/pbx/directory", json={"entries": [{"ext": "2000", "mobile": "not a number"}]}).json()["entries"] == 1
    again = [{"ext": "2000", "mobile": "+353 87 123 4567", "ddi": "061 555 200", "department": "Accounts"}]
    op.put("/api/pbx/directory", json={"entries": again})
    assert op.put("/api/pbx/directory", json={"entries": again}).json()["changed"] is False
