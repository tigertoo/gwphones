"""The video add-on on statements: marked each month it's on, at the
tenant's price, billed to the tenant's add-on customer, locked by a
statement and released by cancelling it; never the main tenant's."""
import json

import pytest
from fastapi.testclient import TestClient

from test_portal_caller import PORTAL, two  # noqa: F401

MONTH = "2026-10"  # ended: statements can be issued for it


@pytest.fixture
def acme(two):
    main, pbx, op, portal, acme = two
    import billing
    acme_op = TestClient(main.app)
    acme_op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": str(acme)})
    acme_op.cookies.set("pbx_tenant", str(acme))
    assert op.put("/api/billing/statement-settings", json={"business_name": "GW Phones"}).status_code == 200
    return main, pbx, op, acme_op, acme, billing


def video(op, tid, on=True, price=None):
    body = {"name": "Acme" if tid != 1 else "Main", "status": "active", "video": on}
    if price is not None:
        body["video_price"] = price
    r = op.put(f"/api/pbx/tenants/{tid}", json=body)
    assert r.status_code == 200, r.text


def months(billing):
    return [dict(r) for r in billing.get_db().execute("SELECT * FROM addon_months ORDER BY tenant_id, month")]


def account(op, tid):
    """The tenant's billing account (made with the tenant)."""
    rows = [c for c in op.get("/api/billing/customers").json()["customers"] if c["tenant_id"] == tid]
    assert len(rows) == 1
    return rows[0]["id"]


def test_marked_at_the_tenants_price_never_the_main_tenant(acme):
    main, pbx, op, acme_op, tid, billing = acme
    video(op, 1, True)  # the operator's own: never charged
    video(op, tid, True, "7.50")
    billing.mark_addons(MONTH)
    rows = months(billing)
    assert [(r["tenant_id"], r["addon"], r["price_micro"]) for r in rows if r["month"] == MONTH] == [(tid, "video", 7_500_000)]
    assert not any(r["tenant_id"] == 1 for r in rows)
    assert op.put(f"/api/pbx/tenants/{tid}", json={"name": "Acme", "video_price": "abc"}).status_code == 400


def test_on_any_time_in_the_month_is_billed(acme):
    main, pbx, op, acme_op, tid, billing = acme
    video(op, tid, True)
    billing.mark_addons(MONTH)
    video(op, tid, False)  # off again before the statement
    billing.mark_addons(MONTH)
    assert [r["month"] for r in months(billing) if r["month"] == MONTH] == [MONTH]


def test_on_the_statement_then_released_by_cancel(acme):
    main, pbx, op, acme_op, tid, billing = acme
    video(op, tid, True)
    cid = account(op, tid)
    billing.mark_addons(MONTH)
    month = op.get(f"/api/billing/customers/{cid}/month?month={MONTH}").json()
    assert month["addons"][0]["label"] == "Video add-on" and month["totals"]["addon_price"] == "5.00"
    r = op.post("/api/billing/statements", json={"month": MONTH, "customer_id": cid})
    assert r.status_code == 200 and r.json()["issued"][0]["total"] == "5.00"
    sid = r.json()["issued"][0]["id"]
    snap = json.loads(billing.get_db().execute("SELECT snapshot FROM statements WHERE id = ?", (sid,)).fetchone()[0])
    assert snap["addons"] == [{"addon": "video", "label": "Video add-on", "price_micro": 5_000_000}]
    pdf = op.get(f"/api/billing/statements/{sid}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # locked: a price change doesn't touch it, and it isn't billed twice
    video(op, tid, True, "9.00")
    billing.mark_addons(MONTH)
    assert months(billing)[0]["price_micro"] == 5_000_000
    assert op.post("/api/billing/statements", json={"month": MONTH, "customer_id": cid}).json()["issued"] == []
    # cancelled: billable again (at the price now)
    assert op.post(f"/api/billing/statements/{sid}/cancel").status_code == 200
    billing.mark_addons(MONTH)
    assert months(billing)[0]["statement_id"] is None and months(billing)[0]["price_micro"] == 9_000_000


def test_every_tenant_is_one_account(acme):
    main, pbx, op, acme_op, tid, billing = acme
    customers = op.get("/api/billing/customers").json()["customers"]
    assert [(c["tenant_id"], c["name"]) for c in customers] == [(tid, "Acme")]  # none for the main tenant
    cid = customers[0]["id"]
    assert op.post("/api/billing/customers", json={"name": "Extra"}).status_code == 400
    assert op.delete(f"/api/billing/customers/{cid}").status_code == 400
    r = op.put(f"/api/billing/customers/{cid}", json={"name": "Acme Ltd", "email": "accounts@acme.example",
                                                       "markup_pct": "20"})
    assert r.status_code == 200
    c = op.get("/api/billing/customers").json()["customers"][0]
    assert (c["name"], c["email"], c["markup_pct"], c["tenant"]["name"]) == ("Acme Ltd", "accounts@acme.example", "20", "Acme")
    # a new tenant gets its own at once
    assert op.post("/api/pbx/tenants", json={"ext_digits": "3", "name": "Beta", "outbound_cid": "015558888"}).status_code == 200
    assert {c["name"] for c in op.get("/api/billing/customers").json()["customers"]} == {"Acme Ltd", "Beta"}


def test_the_main_tenant_is_never_billed(acme):
    main, pbx, op, acme_op, tid, billing = acme
    conn = billing.get_db()
    conn.execute("INSERT INTO calls (uniqueid, dstchannel, start_utc, billsec, direction, ext, status, cost_eur_micro, "
                 "tenant_id) VALUES ('m1', 'x', ?, 60, 'out', '201', 'rated', 50000, 1)", (f"{MONTH}-05T10:00:00+00:00",))
    conn.commit()
    summary = op.get(f"/api/billing/summary?month={MONTH}").json()["customers"]
    main_row = next(c for c in summary if c["customer"]["id"] is None)
    assert main_row["customer"]["name"] == "Not billed (main tenant)" and main_row["totals"]["calls"] == 1
    issued = op.post("/api/billing/statements", json={"month": MONTH}).json()["issued"]
    assert issued == []  # the main tenant's call is never on a statement


def test_old_customers_migrate(acme):
    """Before tenants were customers: the main tenant's customers (test
    data) go with their extension lists and numbers; a tenant with two keeps
    its first, which takes the other's numbers."""
    main, pbx, op, acme_op, tid, billing = acme
    conn = billing.get_db()
    conn.execute("DROP INDEX customers_one_per_tenant")
    conn.execute("INSERT INTO customers (id, name, created_at, tenant_id) VALUES (900, 'Old main', 'x', 1)")
    conn.execute("INSERT INTO customers (id, name, created_at, tenant_id) VALUES (901, 'Acme extra', 'x', ?)", (tid,))
    conn.execute("INSERT INTO customer_exts (ext, customer_id, tenant_id) VALUES ('201', 900, 1)")
    for did, cid in (("35311111111", 900), ("35322222222", 901)):
        conn.execute("INSERT INTO ddis (did, customer_id, monthly_cost_micro, from_month) VALUES (?, ?, 1000000, '2026-10')",
                     (did, cid))
    conn.execute("DELETE FROM state WHERE key = 'one_customer_per_tenant'")
    conn.commit()
    conn = billing.get_db()  # runs the one-off again
    first = account(op, tid)
    assert {r["id"] for r in conn.execute("SELECT id FROM customers")} == {first}
    assert [tuple(r) for r in conn.execute("SELECT did, customer_id FROM ddis")] == [("35322222222", first)]
    assert conn.execute("SELECT COUNT(*) FROM customer_exts").fetchone()[0] == 0


def test_old_statements_without_addons_still_render(env):
    import statements
    snap = {"number": "S00001", "month": MONTH, "issued": "01 Nov 2026",
            "business": {"business_name": "GW", "address": "", "email": "", "phone": "", "payment_details": "",
                         "footer": ""},
            "customer": {"name": "Old", "email": ""}, "ddis": [], "groups": [], "calls": [],
            "totals": {"ddis": 0, "calls": 0, "subtotal": 0, "vat": 0, "total": 0}}
    assert statements.statement_pdf(snap).startswith(b"%PDF")
