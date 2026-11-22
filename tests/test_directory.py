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
    assert "directory_setting.remote_phone_book.enable = 1" in cfg  # on the Directory key, not only in the menu
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


def test_outside_contacts_on_the_phones_and_for_incoming_calls(env, monkeypatch):
    op, pbx, tid, code, o, k = tenant_with_people(env, monkeypatch)
    assert op.post("/api/pbx/contacts", json={"name": "John Smith", "company": "Acme Supplies",
                                              "work": "061 555 1234"}).status_code == 200
    assert op.post("/api/pbx/contacts", json={"company": "The Bakery", "mobile": "+353 86 999 8888"}).status_code == 200
    assert op.post("/api/pbx/contacts", json={"name": "No number"}).status_code == 400
    assert op.post("/api/pbx/contacts", json={"name": "Bad", "work": "call me"}).status_code == 400
    # a contact with a staff member's number doesn't rename them
    op.post("/api/pbx/contacts", json={"name": "Imposter", "mobile": "087 123 4567"})
    import directory
    with pbx.get_db() as conn:
        books = directory.books(conn, tid, o)
        assert [b["name"] for b in books] == ["Ogonnolloe", "Killaloe", "Contacts"]
        xml = directory.book_xml(conn, tid, books[2])
        names = directory.caller_names(conn, tid)
    assert "<Name>John Smith (Acme Supplies)</Name>" in xml
    assert '<Telephone label="Work">0615551234</Telephone>' in xml and "<Name>The Bakery</Name>" in xml
    assert names["353615551234"] == "John Smith (Acme Supplies)" and names["353869998888"] == "The Bakery"
    assert names["353871234567"] == "Cara Murphy (mobile)"  # the person, not the contact
    dial = open(os.path.join(pbx.GENERATED_DIR, "extensions_portal.conf")).read()
    assert "exten => 0615551234,1,Set(CALLERID(name)=John Smith (Acme Supplies))" in dial
    # edit and remove
    cid = next(c["id"] for c in op.get("/api/pbx/contacts").json()["contacts"] if c["company"] == "The Bakery")
    assert op.put(f"/api/pbx/contacts/{cid}", json={"company": "The Bakery", "mobile": "0869990000"}).status_code == 200
    assert op.delete(f"/api/pbx/contacts/{cid}").status_code == 200
    assert all(c["company"] != "The Bakery" for c in op.get("/api/pbx/contacts").json()["contacts"])


def test_microsoft_contacts_replace_only_their_own(env, monkeypatch):
    op, pbx, tid, code, o, k = tenant_with_people(env, monkeypatch)
    op.post("/api/pbx/contacts", json={"name": "Mine", "work": "061 555 0001"})
    r = op.put("/api/pbx/contacts/microsoft", json={"contacts": [
        {"name": "Ann Lee", "company": "Lee Ltd", "work": "+353 1 555 2000"},
        {"name": "Nobody", "company": "", "work": ""}]})  # no number: left out
    assert r.json()["contacts"] == 1
    listed = op.get("/api/pbx/contacts").json()["contacts"]
    assert {(c["name"], c["source"]) for c in listed} == {("Mine", "portal"), ("Ann Lee", "microsoft")}
    ms_id = next(c["id"] for c in listed if c["source"] == "microsoft")
    assert op.put(f"/api/pbx/contacts/{ms_id}", json={"name": "Changed", "work": "015552000"}).status_code == 409
    assert op.put("/api/pbx/contacts/microsoft", json={"contacts": []}).json()["changed"] is True
    assert [c["name"] for c in op.get("/api/pbx/contacts").json()["contacts"]] == ["Mine"]
