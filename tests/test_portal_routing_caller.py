"""Ring groups, IVR menus (and greetings) and business hours for the
portal caller: across two tenants that both have a group 600, menu 500
and hours 400, every change touches only the portal's tenant, and nothing
of the other tenant can be named as a destination."""
import os

import pytest

from test_portal_caller import PORTAL, two  # noqa: F401  (the two-tenant fixture)

MAIN_GREETING, ACME_GREETING = b"\x11" * 8000, b"\x22" * 8000


@pytest.fixture
def routing(two):
    main, pbx, op, portal, acme = two
    import hours, ivr
    for tid in (1, acme):
        token = pbx._current_tenant.set(tid)
        try:
            pbx.api_create_group(pbx.RingGroupRequest(number="600", name=f"T{tid} group", members=["201", "202"]))
            ivr.api_create_menu(ivr.MenuRequest(number="500", name=f"T{tid} menu", options={"1": "201", "2": "600"}))
            hours.api_create_hours(hours.HoursRequest(number="400", name=f"T{tid} hours", week={"mon": [["09:00", "17:00"]]},
                                                      open_target="600", closed_target="vm:201"))
            ivr.write_ulaw(pbx.greeting_path("500", pbx.tenant_names(pbx.get_db(), tid)),
                           MAIN_GREETING if tid == 1 else ACME_GREETING)
            if tid == 1:  # the main tenant's alone
                pbx.api_create_group(pbx.RingGroupRequest(number="601", name="Main only", members=["203"]))
                pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35315550000", target="500", label="Main line"))
        finally:
            pbx._current_tenant.reset(token)
    return main, pbx, op, portal, acme


def row(pbx, table, tid, number):
    r = pbx.get_db().execute(f"SELECT * FROM {table} WHERE tenant_id = ? AND number = ?", (tid, number)).fetchone()
    return dict(r) if r else None


def test_routing_list_is_the_tenants(routing):
    main, pbx, op, portal, acme = routing
    r = portal.get("/api/pbx/routing").json()
    assert [g["name"] for g in r["ring_groups"]] == [f"T{acme} group"]
    assert [m["name"] for m in r["ivr_menus"]] == [f"T{acme} menu"] and r["ivr_menus"][0]["greeting"]
    assert [h["name"] for h in r["business_hours"]] == [f"T{acme} hours"]
    values = {d["value"] for d in r["destinations"]}
    assert {"201", "202", "600", "500", "400", "vm:201", "hangup"} <= values
    assert not values & {"203", "601", "vm:203"}  # the main tenant's alone
    assert r["numbers"] == []  # the main tenant's number isn't Acme's
    assert "Main" not in str(r) and "T1 " not in str(r)


@pytest.mark.parametrize("path,body", [
    ("/api/pbx/ivr", {"name": "Bad", "options": {"1": "203"}}),
    ("/api/pbx/ivr", {"name": "Bad", "options": {"1": "vm:203"}}),
    ("/api/pbx/ivr", {"name": "Bad", "options": {"1": "201"}, "fallback": "601"}),
    ("/api/pbx/hours", {"name": "Bad", "week": {"mon": [["09:00", "17:00"]]}, "open_target": "601",
                        "closed_target": "hangup"}),
    ("/api/pbx/hours", {"name": "Bad", "week": {"mon": [["09:00", "17:00"]]}, "open_target": "201",
                        "closed_target": "vm:203"}),
    ("/api/pbx/ringgroups", {"name": "Bad", "members": ["201", "203"]}),
    ("/api/pbx/ringgroups", {"name": "Bad", "members": ["201"], "voicemail": "203"}),
])
def test_other_tenants_destinations_refused(routing, path, body):
    main, pbx, op, portal, acme = routing
    r = portal.post(path, json=body)
    assert r.status_code == 400, (path, body, r.status_code, r.text)


def test_group_change_is_the_tenants(routing):
    main, pbx, op, portal, acme = routing
    before = row(pbx, "ring_groups", 1, "600")
    r = portal.put("/api/pbx/ringgroups/600", json={"name": "Sales", "members": ["202"], "strategy": "hunt"})
    assert r.status_code == 200
    assert row(pbx, "ring_groups", acme, "600")["name"] == "Sales" and row(pbx, "ring_groups", 1, "600") == before
    assert portal.put("/api/pbx/ringgroups/601", json={"name": "X", "members": ["201"]}).status_code == 404


def test_menu_change_and_delete_are_the_tenants(routing):
    main, pbx, op, portal, acme = routing
    before = row(pbx, "ivr_menus", 1, "500")
    assert portal.put("/api/pbx/ivr/500", json={"name": "Welcome", "options": {"1": "202"}}).status_code == 200
    assert row(pbx, "ivr_menus", acme, "500")["name"] == "Welcome" and row(pbx, "ivr_menus", 1, "500") == before
    # The main tenant's number points at its own 500: that mustn't hold Acme's.
    assert portal.delete("/api/pbx/ivr/500").status_code == 200
    assert row(pbx, "ivr_menus", acme, "500") is None and row(pbx, "ivr_menus", 1, "500") == before


def test_in_use_check_is_the_tenants(routing):
    main, pbx, op, portal, acme = routing
    # Acme's own menu and hours send calls to Acme's 600: that holds it,
    # and the answer names Acme's, never the main tenant's
    r = portal.delete("/api/pbx/ringgroups/600")
    assert r.status_code == 409 and f"T{acme} " in r.json()["detail"] and "T1 " not in r.json()["detail"]


def test_hours_and_override_are_the_tenants(routing):
    main, pbx, op, portal, acme = routing
    before = row(pbx, "business_hours", 1, "400")
    assert portal.put("/api/pbx/hours/400/override", json={"override": "closed"}).status_code == 200
    assert row(pbx, "business_hours", acme, "400")["override"] == "closed" and row(pbx, "business_hours", 1, "400") == before
    assert portal.put("/api/pbx/hours/400", json={"name": "Office", "week": {"tue": [["08:00", "12:00"]]},
                                                  "open_target": "201", "closed_target": "hangup"}).status_code == 200
    assert row(pbx, "business_hours", acme, "400")["name"] == "Office" and row(pbx, "business_hours", 1, "400") == before
    assert portal.delete("/api/pbx/hours/400").status_code == 200
    assert row(pbx, "business_hours", acme, "400") is None and row(pbx, "business_hours", 1, "400") == before


def test_greetings_are_the_tenants(routing):
    main, pbx, op, portal, acme = routing
    conn = pbx.get_db()
    mine = pbx.greeting_path("500", pbx.tenant_names(conn, acme))
    theirs = pbx.greeting_path("500", pbx.tenant_names(conn, 1))
    assert portal.get("/api/pbx/ivr/500/greeting").content == ACME_GREETING
    new = b"\x33" * 16000
    assert portal.put("/api/pbx/ivr/500/greeting", content=new,
                      headers={"Content-Type": "application/octet-stream"}).status_code == 200
    assert open(mine, "rb").read() == new and open(theirs, "rb").read() == MAIN_GREETING
    assert portal.delete("/api/pbx/ivr/500/greeting").status_code == 200
    assert not os.path.exists(mine) and open(theirs, "rb").read() == MAIN_GREETING


def test_suspended_routing_read_only(routing):
    main, pbx, op, portal, acme = routing
    op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "status": "suspended"})
    assert portal.get("/api/pbx/routing").status_code == 200
    assert portal.get("/api/pbx/ivr/500/greeting").status_code == 200
    assert portal.put("/api/pbx/hours/400/override", json={"override": "open"}).status_code == 403
    assert portal.delete("/api/pbx/ivr/500/greeting").status_code == 403


# -- where the tenant's own numbers go -----------------------------------------------------------

@pytest.fixture
def numbers(routing):
    main, pbx, op, portal, acme = routing
    token = pbx._current_tenant.set(acme)
    try:
        pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35315559999", target="201", label="Acme main"))
    finally:
        pbx._current_tenant.reset(token)
    return routing


def route(pbx, did):
    return dict(pbx.get_db().execute("SELECT * FROM inbound_routes WHERE did = ?", (did,)).fetchone())


def test_number_target_is_the_tenants(numbers):
    main, pbx, op, portal, acme = numbers
    theirs = route(pbx, "35315550000")
    r = portal.put("/api/pbx/inbound/35315559999/target", json={"target": "500"})
    assert r.status_code == 200
    mine = route(pbx, "35315559999")
    assert mine["target"] == "500" and mine["label"] == "Acme main" and mine["tenant_id"] == acme
    assert route(pbx, "35315550000") == theirs
    assert [n["target"] for n in portal.get("/api/pbx/routing").json()["numbers"]] == ["500"]


def test_number_of_another_tenant_or_nobody_not_found(numbers):
    main, pbx, op, portal, acme = numbers
    theirs = route(pbx, "35315550000")
    assert portal.put("/api/pbx/inbound/35315550000/target", json={"target": "201"}).status_code == 404
    assert portal.put("/api/pbx/inbound/35318880000/target", json={"target": "201"}).status_code == 404
    assert route(pbx, "35315550000") == theirs
    assert pbx.get_db().execute("SELECT 1 FROM inbound_routes WHERE did = '35318880000'").fetchone() is None


def test_numbers_cant_be_added_or_removed(numbers):
    main, pbx, op, portal, acme = numbers
    assert portal.post("/api/pbx/inbound", json={"did": "35318880000", "target": "201"}).status_code == 403
    assert portal.delete("/api/pbx/inbound/35315559999").status_code == 403


@pytest.mark.parametrize("target", ["203", "601", "vm:201", "hangup", ""])
def test_number_target_must_be_the_tenants_and_a_place(numbers, target):
    main, pbx, op, portal, acme = numbers
    assert portal.put("/api/pbx/inbound/35315559999/target", json={"target": target}).status_code == 400
    assert route(pbx, "35315559999")["target"] == "201"


# -- call links ------------------------------------------------------------------------------------

@pytest.fixture
def links(routing):
    """Each tenant has a call link to its own 600; the main tenant's has video."""
    main, pbx, op, portal, acme = routing
    import calllinks
    ids = {}
    for tid, video in ((1, True), (acme, False)):
        token = pbx._current_tenant.set(tid)
        try:
            calllinks.api_add_call_link(calllinks.CallLinkRequest(label=f"T{tid} website", target="600", video=video))
        finally:
            pbx._current_tenant.reset(token)
        ids[tid] = pbx.get_db().execute("SELECT id FROM call_links WHERE tenant_id = ?", (tid,)).fetchone()["id"]
    return routing, ids


def link(pbx, link_id):
    r = pbx.get_db().execute("SELECT * FROM call_links WHERE id = ?", (link_id,)).fetchone()
    return dict(r) if r else None


def test_call_links_list_is_the_tenants(links):
    (main, pbx, op, portal, acme), ids = links
    r = portal.get("/api/pbx/call-links").json()["links"]
    assert [l["label"] for l in r] == [f"T{acme} website"] and r[0]["url"].endswith(link(pbx, ids[acme])["token"])
    assert link(pbx, ids[1])["token"] not in str(r)


def test_call_link_changes_are_the_tenants(links):
    (main, pbx, op, portal, acme), ids = links
    theirs = link(pbx, ids[1])
    body = {"label": "X", "target": "201"}
    assert portal.put(f"/api/pbx/call-links/{ids[1]}", json=body).status_code == 404
    assert portal.post(f"/api/pbx/call-links/{ids[1]}/new-url").status_code == 404
    assert portal.delete(f"/api/pbx/call-links/{ids[1]}").status_code == 404
    assert link(pbx, ids[1]) == theirs
    old = link(pbx, ids[acme])["token"]
    assert portal.post(f"/api/pbx/call-links/{ids[acme]}/new-url").status_code == 200
    assert link(pbx, ids[acme])["token"] != old and link(pbx, ids[1]) == theirs
    assert portal.put(f"/api/pbx/call-links/{ids[acme]}", json={"label": "Shop", "target": "500"}).status_code == 200
    assert link(pbx, ids[acme])["label"] == "Shop" and link(pbx, ids[1]) == theirs
    assert portal.delete(f"/api/pbx/call-links/{ids[acme]}").status_code == 200
    assert link(pbx, ids[acme]) is None and link(pbx, ids[1]) == theirs


@pytest.mark.parametrize("target", ["203", "601", "vm:201", "hangup"])
def test_call_link_target_must_be_the_tenants(links, target):
    (main, pbx, op, portal, acme), ids = links
    assert portal.post("/api/pbx/call-links", json={"label": "Bad", "target": target}).status_code == 400
