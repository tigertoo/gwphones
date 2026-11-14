"""Caller-ID-aware routing (decided 2026-11-12): a call only goes out on
routes that can show its caller ID; if none can, the tenant's main number
is shown instead; if not even that, it's refused. Empty lists change
nothing."""
import re

import pytest
from fastapi.testclient import TestClient

IE, AU, UK = "+35315550000", "+61291234567", "+442071234567"
VOXBEAM_KEYS = ("voxbeam0011101", "voxbeam0011102", "voxbeam0011103", "voxbeam0011104")


@pytest.fixture
def lcr(env, monkeypatch):
    """Every route priced the same (so only the caller-ID rule decides),
    mobiles on "cheapest" over all routes, and an operator client."""
    import main, pbx, lcr as lcr_module
    row = {"currency": "EUR", "conn_unit": 1, "bill_unit": 1, "conn_fee_micro": 0, "destination": "Test",
           "prefix": "353", "expires_at": None}
    monkeypatch.setattr(lcr_module, "rate_for", lambda conn, trunk, route, number, at: ({"id": 1}, row, 10000, ""))
    monkeypatch.setattr(lcr_module, "latest_fx", lambda conn, currency: (1, "now"))
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    pbx.api_create_extension(pbx.ExtensionCreate(ext="201", name="Reception"))  # incoming calls' default
    import billing
    billing.get_db().close()  # the route check reads it (read only), so it must exist
    s = op.get("/api/pbx/config").json()["settings"]

    def save(cli_countries=None, outbound_cid="015550000"):
        body = {**s, "outbound_cid": outbound_cid, "routes": {**s["routes"], "mobile": "cheapest"}}
        if cli_countries is not None:
            body["cli_countries"] = cli_countries
        r = op.put("/api/pbx/settings", json=body)
        assert r.status_code == 200, r.text
    save()
    return pbx, lcr_module, op, save


def routes(d):
    return [f"{r['trunk']}{r['route']}" for r in d["routes"]]


def test_no_lists_changes_nothing(lcr):
    pbx, lcr_module, op, save = lcr
    for cli in (IE, AU, UK, ""):
        d = lcr_module.decide("mobile", "353871234567", 1, cli)
        assert routes(d) == ["irishvoip", *VOXBEAM_KEYS] and "cli" not in d and "refuse" not in d


def test_caller_id_picks_the_routes(lcr):
    pbx, lcr_module, op, save = lcr
    save({"irishvoip": "353", **{k: "61" for k in VOXBEAM_KEYS}})
    assert routes(lcr_module.decide("mobile", "353871234567", 1, IE)) == ["irishvoip"]
    assert routes(lcr_module.decide("mobile", "353871234567", 1, AU)) == list(VOXBEAM_KEYS)
    assert routes(lcr_module.decide("mobile", "353871234567", 1, "0871234567")) == ["irishvoip"]  # national form
    assert routes(lcr_module.decide("mobile", "353871234567", 1, "0061291234567")) == list(VOXBEAM_KEYS)


def test_one_voxbeam_route_only(lcr):
    pbx, lcr_module, op, save = lcr
    # only Platinum keeps an Australian caller ID (an empty list would mean any)
    save({"irishvoip": "353", "voxbeam0011101": "353", "voxbeam0011102": "61,353", "voxbeam0011103": "353",
          "voxbeam0011104": "353"})
    assert routes(lcr_module.decide("mobile", "353871234567", 1, AU)) == ["voxbeam0011102"]
    assert set(routes(lcr_module.decide("mobile", "353871234567", 1, IE))) == {"irishvoip", *VOXBEAM_KEYS}


def test_falls_back_to_the_main_number(lcr):
    pbx, lcr_module, op, save = lcr
    save({"irishvoip": "353", **{k: "61" for k in VOXBEAM_KEYS}})
    d = lcr_module.decide("mobile", "353871234567", 1, UK)
    assert routes(d) == ["irishvoip"] and d["cli"] == "+35315550000" and "main number" in d["cli_note"]
    assert any(c.get("dropped", "").startswith("Can't show caller ID +44") for c in d["candidates"])


def test_refused_when_not_even_the_main_number_fits(lcr):
    pbx, lcr_module, op, save = lcr
    save({k: "61" for k in ("irishvoip", *VOXBEAM_KEYS)})
    d = lcr_module.decide("mobile", "353871234567", 1, UK)
    assert d["routes"] == [] and "+442071234567" in d["refuse"] and "+35315550000" in d["refuse"]


def test_each_tenant_its_own_main_number(lcr):
    pbx, lcr_module, op, save = lcr
    save({"irishvoip": "353", **{k: "61" for k in VOXBEAM_KEYS}})
    assert op.post("/api/pbx/tenants", json={"name": "Acme", "outbound_cid": "+61291112222"}).status_code == 200
    acme = pbx.get_db().execute("SELECT id FROM tenants WHERE name = 'Acme'").fetchone()["id"]
    d = lcr_module.decide("mobile", "353871234567", acme, UK)
    assert d["cli"] == "+61291112222" and routes(d) == list(VOXBEAM_KEYS)  # Acme's, never the main tenant's


def test_the_answer_asterisk_gets(lcr):
    pbx, lcr_module, op, save = lcr
    save({"irishvoip": "353", **{k: "61" for k in VOXBEAM_KEYS}})
    import asyncio
    import httpx
    import main
    token = pbx.get_db().execute("SELECT value FROM settings WHERE key = 'lcr_token'").fetchone()["value"]

    def ask(cli):  # as Asterisk asks: from this machine, with the token
        async def go():
            transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                r = await c.get("/lcr/route", params={"kind": "mobile", "num": "353871234567", "cli": cli},
                                headers={"X-LCR-Token": token})
                return r.text
        return asyncio.run(go())
    assert ask(IE) == "OK:irishvoip"
    assert ask(UK) == "OK:irishvoip|CLI:+35315550000"
    assert ask(AU).startswith("OK:0011101/")
    # an unreadable caller ID never refuses a call: no caller-ID rule, logged
    assert ask("not a number") == "OK:irishvoip/0011101/0011102/0011103/0011104"
    log = open(pbx.CHANGES_LOG_PATH).read()
    assert "isn't a number, so no caller-ID rule applied" in log
    # the fallback is logged for good, not only in the console's recent list
    assert "the main number +35315550000 is shown instead" in log


def test_settings_checked(lcr):
    pbx, lcr_module, op, save = lcr
    s = op.get("/api/pbx/config").json()["settings"]
    for bad in ({"irishvoip": "abc"}, {"irishvoip": "0353"}, {"nope": "353"}, {"irishvoip": "35312"}):
        assert op.put("/api/pbx/settings", json={**s, "cli_countries": bad}).status_code == 400, bad
    assert op.put("/api/pbx/settings", json={**s, "cli_countries": {"irishvoip": "+353, 61,353"}}).status_code == 200
    assert op.get("/api/pbx/config").json()["settings"]["cli_countries"]["irishvoip"] == "353,61"


def test_dialplan_only_once_a_list_is_set(lcr):
    pbx, lcr_module, op, save = lcr
    from test_ceilings import jumps_bad
    gen = open(pbx.PORTAL_DIALPLAN).read()
    assert "&cli=" not in gen and "NEWCID" not in gen
    save({"irishvoip": "353"})
    gen = open(pbx.PORTAL_DIALPLAN).read()
    assert gen.count("&cli=${URIENCODE(${OUT_CID})}") == 4  # mobile, landline, freephone, international
    assert '?Set(CALLERID(num)=${NEWCID:4}))' in gen and not jumps_bad(gen)
    # set before the route check, so the check sees the number the call would show
    block = gen.split("exten => mobile,1")[1].split("exten => ")[0]
    assert block.index("Set(OUT_CID=") < block.index("&cli=")
    save({"irishvoip": ""})
    assert "NEWCID" not in open(pbx.PORTAL_DIALPLAN).read()


def test_emergency_calls_never_show_what_irish_voip_cant(lcr):
    """112/999 skip the route check and always use Irish VoIP: an extension
    whose own caller ID Irish VoIP can't show uses the tenant's main number
    there, and its ordinary calls still go through the route check."""
    pbx, lcr_module, op, save = lcr
    pbx.api_create_extension(pbx.ExtensionCreate(ext="202", name="Sydney desk"))
    assert op.put("/api/pbx/extensions/202/outbound-cid", json={"outbound_cid": AU}).status_code == 200
    assert op.put("/api/pbx/extensions/201/outbound-cid", json={"outbound_cid": "+35315551111"}).status_code == 200
    save({"irishvoip": "353", **{k: "61" for k in VOXBEAM_KEYS}})
    ext_conf = open(pbx.EXT_CONF).read()
    sydney = ext_conf.split("[202](endpoint-ext)")[1].split("\n[")[0]
    dublin = ext_conf.split("[201](endpoint-ext)")[1].split("\n[")[0]
    assert "set_var=EMERG_OWN_CID=0" in sydney and "EMERG_OWN_CID" not in dublin
    gen = open(pbx.PORTAL_DIALPLAN).read()
    for n in ("112", "999"):
        line = [l for l in gen.splitlines() if l.startswith(f"exten => {n},1,Set(OUT_CID=")][0]
        assert '"${EMERG_OWN_CID}" != "0"' in line and line.endswith(":${OUTBOUND_CID})})")
    assert "\nOUTBOUND_CID=015550000\n" in gen  # the main number
    assert "&cli=${URIENCODE(${OUT_CID})}" in gen  # ordinary calls: the route check, as before


def test_no_emergency_change_without_an_irish_voip_list(lcr):
    pbx, lcr_module, op, save = lcr
    pbx.api_create_extension(pbx.ExtensionCreate(ext="202", name="Sydney desk"))
    op.put("/api/pbx/extensions/202/outbound-cid", json={"outbound_cid": AU})
    save({k: "61" for k in VOXBEAM_KEYS})  # Voxbeam lists only: Irish VoIP shows anything
    assert "EMERG_OWN_CID" not in open(pbx.EXT_CONF).read() + open(pbx.PORTAL_DIALPLAN).read()
