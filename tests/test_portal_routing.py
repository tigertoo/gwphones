"""The portal's ring groups, IVR menus (and greetings) and business hours:
admins only, and against the real gwphones app."""
import os
import re

from test_portal_phone import PW, world  # noqa: F401  (the two-tenant world)


def test_staff_refused_everywhere_here(world):
    """Every route of the routing module, GETs included, refuses staff."""
    from gwportal import routing
    ann = world["client"]("ann")
    tried = 0
    for route in routing.router.routes:
        for method in route.methods - {"HEAD"}:
            path = re.sub(r"\{[^}]+\}", "500", route.path)
            if path.endswith("/greeting") and method == "PUT":
                r = ann.put(path, content=b"\xff" * 8000)
            else:
                r = ann.request(method, path, json={"name": "x"})
            assert r.status_code == 403, (method, route.path, r.status_code)
            tried += 1
    assert tried >= 14


def test_build_a_call_flow(world):
    admin = world["client"]("admin")
    g = admin.post("/api/groups", json={"name": "Office", "members": ["201", "202"]})
    assert g.status_code == 200 and g.json()["number"] == "600"
    m = admin.post("/api/menus", json={"name": "Welcome", "options": {"1": "600", "2": "vm:201"}, "fallback": "hangup"})
    assert m.status_code == 200 and m.json()["number"] == "500"
    h = admin.post("/api/hours", json={"name": "Opening hours", "week": {"mon": [["09:00", "17:30"]]},
                                       "open_target": "500", "closed_target": "vm:201"})
    assert h.status_code == 200 and h.json()["number"] == "400"
    r = admin.get("/api/routing").json()
    assert [x["number"] for x in r["ring_groups"]] == ["600"] and r["ivr_menus"][0]["options"] == {"1": "600", "2": "vm:201"}
    assert r["business_hours"][0]["open_target"] == "500"
    values = {d["value"] for d in r["destinations"]}
    assert {"201", "202", "600", "500", "400", "vm:201", "hangup"} <= values and "203" not in values
    assert admin.put("/api/hours/400/override", json={"override": "closed"}).status_code == 200
    assert admin.get("/api/routing").json()["business_hours"][0]["override"] == "closed"
    # in use: gwphones' plain answer is passed on
    r = admin.delete("/api/groups/600")
    assert r.status_code == 409 and "Change that first" in r.json()["detail"]
    assert admin.delete("/api/hours/400").status_code == 200
    assert admin.delete("/api/menus/500").status_code == 200 and admin.delete("/api/groups/600").status_code == 200


def test_other_tenants_destinations_refused(world):
    admin = world["client"]("admin")
    r = admin.post("/api/menus", json={"name": "Bad", "options": {"1": "203"}})  # only the main tenant has 203
    assert r.status_code == 400 and "203" in r.json()["detail"]


def test_greeting(world, monkeypatch):
    pbx, acme = world["pbx"], world["acme"]["id"]
    admin = world["client"]("admin")
    admin.post("/api/menus", json={"name": "Welcome", "options": {"1": "201"}})
    audio = b"\x55" * 24000
    r = admin.put("/api/menus/500/greeting", content=audio, headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200
    path = pbx.greeting_path("500", pbx.tenant_names(pbx.get_db(), acme))
    assert open(path, "rb").read() == audio
    assert admin.get("/api/menus/500/greeting").content == audio
    assert admin.get("/api/routing").json()["ivr_menus"][0]["greeting"] is True
    assert admin.delete("/api/menus/500/greeting").status_code == 200 and not os.path.exists(path)


def test_greeting_size_checked_first(world, monkeypatch):
    from gwportal import gw
    admin = world["client"]("admin")
    admin.post("/api/menus", json={"name": "Welcome", "options": {"1": "201"}})
    calls = []
    real = gw.call
    monkeypatch.setattr(gw, "call", lambda *a, **k: calls.append(a) or real(*a, **k))
    r = admin.put("/api/menus/500/greeting", content=b"\xff" * (120 * 8000 + 1),
                  headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400 and "2 minutes" in r.json()["detail"] and not calls
    r = admin.put("/api/menus/500/greeting", content=b"\xff" * 100, headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400 and "too short" in r.json()["detail"] and not calls


def test_number_target(world):
    pbx, acme = world["pbx"], world["acme"]["id"]
    token = pbx._current_tenant.set(acme)
    try:
        pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35315559999", target="201", label="Acme main"))
    finally:
        pbx._current_tenant.reset(token)
    admin = world["client"]("admin")
    admin.post("/api/menus", json={"name": "Welcome", "options": {"1": "201"}})
    assert admin.put("/api/numbers/35315559999", json={"target": "500"}).status_code == 200
    assert admin.get("/api/routing").json()["numbers"] == [{"did": "35315559999", "label": "Acme main", "target": "500"}]
    assert admin.put("/api/numbers/35315550000", json={"target": "500"}).status_code == 404  # not Acme's
    assert world["client"]("ann").put("/api/numbers/35315559999", json={"target": "201"}).status_code == 403
