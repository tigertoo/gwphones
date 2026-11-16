"""The portal's billing: a tenant's admins see its own statements, this
month so far and its calls, at its prices; never a cost, markup, rate or
trunk, never another tenant's, never "Not assigned"; staff see none of it."""
import json
import re
from datetime import datetime, timezone

import pytest

from test_portal_phone import PW, world  # noqa: F401  (the two-tenant world)

COST = 123457  # €0.123457: distinctive, so a leak shows
MARKUP = "37.5"
PAST = "2026-10"


@pytest.fixture
def bill(world):
    import billing
    op, acme = world["op"], world["acme"]
    op.headers["X-PBX-Tenant"] = str(acme["id"])
    op.cookies.set("pbx_tenant", str(acme["id"]))
    r = op.post("/api/billing/customers", json={"name": "Acme Ltd", "exts": ["201"], "markup_pct": MARKUP})
    assert r.status_code == 200, r.text
    op.headers["X-PBX-Tenant"] = "1"
    op.cookies.set("pbx_tenant", "1")
    op.post("/api/billing/customers", json={"name": "Main Co", "exts": ["201"]})
    assert op.put("/api/billing/statement-settings", json={"business_name": "GW Phones"}).status_code == 200
    now = billing.datetime.now(billing.LOCAL_TZ).strftime("%Y-%m")
    conn = billing.get_db()
    n = 0

    def call(tenant, ext, month, number="353871234567", status="rated"):
        nonlocal n
        n += 1
        start = f"{month}-05T10:{n:02d}:00+00:00"
        conn.execute("INSERT INTO calls (uniqueid, dstchannel, start_utc, billsec, direction, ext, trunk, route, number, "
                     "status, destination, rate_micro, currency, cost_micro, cost_eur_micro, tenant_id, sheet_id, "
                     "lastdata, channel) VALUES (?, 'x', ?, 65, 'out', ?, 'voxbeam', 'cli', ?, ?, 'Ireland Mobile', "
                     "98765, 'USD', ?, ?, ?, 4, 'PJSIP/secret-trunk', 'PJSIP/1002201')",
                     (f"u{n}", start, ext, number, status, COST, COST if status != "unrated" else None, tenant))
    for month in (now, PAST):
        call(acme["id"], "201", month)
        call(acme["id"], "202", month)  # no customer: "not priced"
        call(1, "201", month, number="35319999999")  # the main tenant's
    call(acme["id"], "201", now, status="unrated")
    conn.commit()
    return now, conn


def admin(world):
    return world["client"]("admin")


def test_staff_refused_everywhere_here(world):
    from gwportal import billing
    ann = world["client"]("ann")
    tried = 0
    for route in billing.router.routes:
        for method in route.methods - {"HEAD"}:
            r = ann.request(method, re.sub(r"\{[^}]+\}", "1", route.path))
            assert r.status_code == 403, (method, route.path)
            tried += 1
    assert tried == 4


def test_month_so_far_prices_only(world, bill):
    now, conn = bill
    a = admin(world)
    r = a.get("/api/billing/month")
    assert r.status_code == 200
    m = r.json()
    assert m["month"] == now and m["month_open"] and [c["customer"] for c in m["customers"]] == ["Acme Ltd"]
    c = m["customers"][0]
    price = round(COST * 1.375 / 1_000_000, 2)
    assert c["calls"]["count"] == 1 and c["total"] == f"{price:.2f}" and c["not_priced"] == 1
    assert c["by_destination"][0]["destination"] == "Ireland Mobile"


def test_calls_are_the_tenants_with_its_price(world, bill):
    now, conn = bill
    a = admin(world)
    calls = a.get("/api/billing/calls").json()["calls"]
    assert {c["ext"] for c in calls} == {"201", "202"}  # not the main tenant's
    assert all("35319999999" not in c["number"] for c in calls)
    by_ext = {c["ext"]: c for c in calls if c["state"] != "not priced" or c["ext"] == "202"}
    assert sum(c["ext"] == "201" and c["state"] == "not priced" for c in calls) == 1  # the unrated one
    assert by_ext["201"]["amount"] == f"{COST * 1.375 / 1_000_000:.3f}" and by_ext["201"]["state"] == "estimate"
    assert by_ext["202"]["amount"] is None and by_ext["202"]["state"] == "not priced"
    only = a.get("/api/billing/calls?ext=202").json()["calls"]
    assert {c["ext"] for c in only} == {"202"}
    assert a.get("/api/billing/calls?month=nope").status_code == 400


def test_statements_and_pdf_only_its_own(world, bill):
    now, conn = bill
    op = world["op"]
    r = op.post("/api/billing/statements", json={"month": PAST})
    assert r.status_code == 200, r.text
    issued = {s["customer"]: s["id"] for s in r.json()["issued"]}
    a = admin(world)
    listed = a.get("/api/billing/statements").json()["statements"]
    assert [s["customer"] for s in listed] == ["Acme Ltd"] and listed[0]["month"] == PAST
    pdf = a.get(f"/api/billing/statements/{issued['Acme Ltd']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["cache-control"] == "no-store"
    assert a.get(f"/api/billing/statements/{issued['Main Co']}/pdf").status_code == 404  # another tenant's
    # cancelled: gone
    op.post(f"/api/billing/statements/{issued['Acme Ltd']}/cancel")
    assert a.get("/api/billing/statements").json()["statements"] == []
    assert a.get(f"/api/billing/statements/{issued['Acme Ltd']}/pdf").status_code == 404
    # the download is audited
    from gwportal import db
    with db.connect() as pconn:
        assert pconn.execute("SELECT COUNT(*) FROM audit WHERE action = 'statement downloaded'").fetchone()[0] == 1


def test_no_cost_markup_rate_or_trunk_anywhere(world, bill):
    now, conn = bill
    world["op"].post("/api/billing/statements", json={"month": PAST})
    a = admin(world)
    allowed = {"statements", "id", "number", "month", "customer", "total", "issued_at", "month_open", "customers",
               "calls", "count", "seconds", "amount", "by_destination", "destination", "numbers", "label", "addons",
               "not_priced", "provisional", "billed_already", "start", "ext", "state", "truncated"}

    def keys(x):
        if isinstance(x, dict):
            return set(x) | set().union(*(keys(v) for v in x.values()))
        if isinstance(x, list):
            return set().union(*(keys(v) for v in x)) if x else set()
        return set()
    for path in ("/api/billing/statements", "/api/billing/month", f"/api/billing/month?month={PAST}",
                 "/api/billing/calls", f"/api/billing/calls?month={PAST}"):
        r = a.get(path)
        assert r.status_code == 200, path
        assert keys(r.json()) <= allowed, (path, keys(r.json()) - allowed)
        text = r.text
        for leak in ("0.123457", "123457", "98765", "0.098765", MARKUP, "voxbeam", "secret-trunk", "USD", "35319999999"):
            assert leak not in text, (path, leak)


def test_not_for_the_portal_without_the_routes(world, bill):
    """gwphones' operator billing stays the console's."""
    from conftest import PORTAL
    import main
    from fastapi.testclient import TestClient
    p = TestClient(main.app)
    p.headers.update({"X-Portal-Secret": "portal-test-secret", "X-Portal-Tenant": world["acme"]["code"]})
    for path in ("/api/billing/summary?month=2026-10", "/api/billing/customers", "/api/billing/statements"):
        assert p.get(path).status_code in (403, 404), path
