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


def customer(c, name, exts=(), bills_addons=False):
    r = c.post("/api/billing/customers", json={"name": name, "exts": list(exts), "bills_addons": bills_addons})
    assert r.status_code == 200, r.text
    return r.json()["id"]


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
    cid = customer(acme_op, "Acme Ltd", ["201"])
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


def test_billed_to_the_marked_customer(acme):
    main, pbx, op, acme_op, tid, billing = acme
    video(op, tid, True)
    first = customer(acme_op, "Acme Sales", ["201"])
    second = customer(acme_op, "Acme Support", ["202"], bills_addons=True)
    billing.mark_addons(MONTH)
    assert op.get(f"/api/billing/customers/{second}/month?month={MONTH}").json()["addons"]
    assert not op.get(f"/api/billing/customers/{first}/month?month={MONTH}").json()["addons"]


def test_no_customer_shows_under_not_assigned(acme):
    main, pbx, op, acme_op, tid, billing = acme
    video(op, tid, True)
    billing.mark_addons(MONTH)
    summary = op.get(f"/api/billing/summary?month={MONTH}").json()["customers"]
    unassigned = next(c for c in summary if c["customer"]["id"] is None)
    assert unassigned["addons"] and unassigned["totals"]["addon_price"] == "5.00"


def test_old_statements_without_addons_still_render(env):
    import statements
    snap = {"number": "S00001", "month": MONTH, "issued": "01 Nov 2026",
            "business": {"business_name": "GW", "address": "", "email": "", "phone": "", "payment_details": "",
                         "footer": ""},
            "customer": {"name": "Old", "email": ""}, "ddis": [], "groups": [], "calls": [],
            "totals": {"ddis": 0, "calls": 0, "subtotal": 0, "vat": 0, "total": 0}}
    assert statements.statement_pdf(snap).startswith(b"%PDF")
