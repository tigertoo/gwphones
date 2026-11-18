"""An extension's caller ID from the portal: one of the tenant's own
numbers or its main number, never anyone else's; an admin for any of the
tenant's extensions, a user for their own; the operator's console still
sets any number."""
from test_portal_phone import ext_row, world  # noqa: F401  (the two-tenant world)


def own_numbers(world):
    pbx, acme = world["pbx"], world["acme"]["id"]
    for tid, did in ((acme, "35315557777"), (acme, "35315558888"), (1, "35316660000")):
        token = pbx._current_tenant.set(tid)
        try:
            pbx.api_set_inbound_route(pbx.InboundRouteRequest(did=did, target="201"))
        finally:
            pbx._current_tenant.reset(token)


def test_choices_are_the_tenants_own(world):
    own_numbers(world)
    d = world["client"]("admin").get("/api/extensions").json()
    assert d["caller_ids"] == {"default": "+35315559999", "numbers": ["+35315557777", "+35315558888"]}
    assert all(e["outbound_cid"] == "" for e in d["extensions"])
    # a user sees the same choices for their own extension
    assert world["client"]("ann").get("/api/extensions").json()["caller_ids"]["numbers"] == ["+35315557777", "+35315558888"]


def test_user_sets_their_own(world):
    own_numbers(world)
    acme = world["acme"]["id"]
    ann = world["client"]("ann")
    r = ann.put("/api/extensions/201/caller-id", json={"outbound_cid": "01 555 7777"})
    assert r.status_code == 200 and r.json()["ok"]
    assert ext_row(world, acme, "201")["outbound_cid"] == "+35315557777"
    conf = open(world["pbx"].EXT_CONF).read()
    assert "set_var=EXT_CID=+35315557777" in conf
    # not someone else's extension
    assert ann.put("/api/extensions/202/caller-id", json={"outbound_cid": "+35315557777"}).status_code == 404
    # back to the main number
    assert ann.put("/api/extensions/201/caller-id", json={"outbound_cid": ""}).status_code == 200
    assert ext_row(world, acme, "201")["outbound_cid"] == ""


def test_never_a_number_that_isnt_theirs(world):
    own_numbers(world)
    acme = world["acme"]["id"]
    admin = world["client"]("admin")
    for other in ("+35316660000",  # the main tenant's
                  "0871234567",    # anybody's mobile
                  "+442071234567", "112", "not a number"):
        r = admin.put("/api/extensions/202/caller-id", json={"outbound_cid": other})
        assert r.status_code == 400, other
    assert ext_row(world, acme, "202")["outbound_cid"] == ""
    # the main number and its own others are fine
    assert admin.put("/api/extensions/202/caller-id", json={"outbound_cid": "+35315558888"}).status_code == 200
    assert admin.put("/api/extensions/202/caller-id", json={"outbound_cid": "015559999"}).status_code == 200
    assert ext_row(world, acme, "202")["outbound_cid"] == "+35315559999"


def test_operator_still_sets_any_number(world):
    acme = world["acme"]
    op = world["op"]
    op.cookies.set("pbx_tenant", str(acme["id"]))  # the console's tenant switcher
    op.headers["X-PBX-Tenant"] = str(acme["id"])
    assert op.put("/api/pbx/extensions/201/outbound-cid", json={"outbound_cid": "+442071234567"}).status_code == 200
    assert ext_row(world, acme["id"], "201")["outbound_cid"] == "+442071234567"
    # shown in the portal as the provider's choice, still one it can change
    d = world["client"]("ann").get("/api/extensions").json()
    assert d["extensions"][0]["outbound_cid"] == "+442071234567"


def test_audited_without_staff_seeing_others(world):
    own_numbers(world)
    world["client"]("ann").put("/api/extensions/201/caller-id", json={"outbound_cid": "+35315557777"})
    from gwportal import db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'caller ID changed'").fetchone()[0] == 1
