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
