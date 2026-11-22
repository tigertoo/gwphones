"""Sites (offices) of one tenant: each with its own number and block of
extensions. An extension without its own caller ID shows its site's
number, to callers and to the emergency services; *8 answers only phones
of the same site; a new extension takes the next number of its site."""
import os

from test_four_digit import setup  # noqa: F401


def numbers(pbx, tid, *dids):
    token = pbx._current_tenant.set(tid)
    try:
        for d in dids:
            pbx.api_set_inbound_route(pbx.InboundRouteRequest(did=d, target="2000"))
    finally:
        pbx._current_tenant.reset(token)


def test_sites_blocks_caller_id_and_pickup(env, monkeypatch):
    op, pbx, tid, code = setup(env, monkeypatch)
    op.post("/api/pbx/extensions", json={"ext": "2000", "name": "Reception"})
    numbers(pbx, tid, "35315551000", "35321555200")
    r = op.post("/api/pbx/sites", json={"name": "Dublin", "number": "01 555 1000", "ext_min": 2000, "ext_max": 2999})
    assert r.status_code == 200, r.text
    dublin = r.json()["id"]
    cork = op.post("/api/pbx/sites", json={"name": "Cork", "number": "+353 21 555 200",
                                           "ext_min": 3000, "ext_max": 3999}).json()["id"]
    # a site's number must be the tenant's own; blocks inside 2000-8999 and not overlapping
    assert op.post("/api/pbx/sites", json={"name": "X", "number": "0871234567"}).status_code == 400
    assert op.post("/api/pbx/sites", json={"name": "Y", "ext_min": 2500, "ext_max": 3500}).status_code == 409
    assert op.post("/api/pbx/sites", json={"name": "Z", "ext_min": 100, "ext_max": 150}).status_code == 400
    assert op.post("/api/pbx/sites", json={"name": "dublin"}).status_code == 409
    # a new extension takes its site's next free number; one outside its block is refused
    r = op.post("/api/pbx/extensions", json={"name": "Cork desk", "site_id": cork})
    assert r.status_code == 200 and r.json()["ext"] == "3000"
    assert op.post("/api/pbx/extensions", json={"ext": "2500", "name": "Wrong", "site_id": cork}).status_code == 400
    assert op.put("/api/pbx/extensions/2000/site", json={"site_id": dublin}).status_code == 200
    assert op.put("/api/pbx/extensions/2000/site", json={"site_id": cork}).status_code == 400
    pjsip = open(pbx.EXT_CONF).read()
    cork_ep = pjsip.split(f"[{code}3000](endpoint-ext)")[1].split("\n\n")[0]
    dub_ep = pjsip.split(f"[{code}2000](endpoint-ext)")[1].split("\n\n")[0]
    assert "set_var=EXT_CID=+35321555200" in cork_ep and "set_var=EXT_CID=+35315551000" in dub_ep
    assert f"named_pickup_group=t{code}-site{cork}" in cork_ep and f"named_pickup_group=t{code}-site{dublin}" in dub_ep
    # its own caller ID still wins
    op.put("/api/pbx/extensions/3000/outbound-cid", json={"outbound_cid": "+35315551000"})
    assert "set_var=EXT_CID=+35315551000" in open(pbx.EXT_CONF).read().split(f"[{code}3000](endpoint-ext)")[1].split("\n\n")[0]
    # forwarded calls use the same (EXTCID_ globals)
    dial = open(os.path.join(pbx.GENERATED_DIR, "extensions_portal.conf")).read()
    assert f"EXTCID_{code}2000=+35315551000" in dial
    # a site with extensions can't go; its block can't shrink past them
    assert op.delete(f"/api/pbx/sites/{cork}").status_code == 409
    assert op.put(f"/api/pbx/sites/{cork}", json={"name": "Cork", "ext_min": 3100, "ext_max": 3999}).status_code == 409
    listed = op.get("/api/pbx/extensions").json()
    ext = {e["ext"]: e for e in listed["extensions"]}
    assert ext["2000"]["site_id"] == dublin and ext["2000"]["default_cid"] == "+35315551000"
    assert {s["name"]: s["extensions"] for s in listed["sites"]} == {"Dublin": 1, "Cork": 1}


def test_site_routes_for_the_portal_and_none_of_another_tenants(env, monkeypatch):
    op, pbx, tid, code = setup(env, monkeypatch)
    sid = op.post("/api/pbx/sites", json={"name": "Dublin"}).json()["id"]
    import main
    assert ("PUT", "/api/pbx/extensions/{ext}/site") in main.PORTAL_ROUTES
    op.cookies.set("pbx_tenant", "1")
    op.headers["X-PBX-Tenant"] = "1"
    assert op.put(f"/api/pbx/sites/{sid}", json={"name": "Mine now"}).status_code == 404
    assert op.delete(f"/api/pbx/sites/{sid}").status_code == 404
