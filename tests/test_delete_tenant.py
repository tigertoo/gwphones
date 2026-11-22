"""Deleting a tenant: only suspended, only with its numbers gone, only with
its code typed again. Its phone system goes, its files are kept 30 days,
its billing account is closed (statements and calls kept), its code is
never used again, and the portal removes its people. Nobody else's
anything changes."""
import os
from datetime import datetime, timedelta, timezone

from test_portal_phone import ext_row, world  # noqa: F401  (the two-tenant world)


def test_delete_a_tenant(world):
    op, pbx, acme = world["op"], world["pbx"], world["acme"]
    tid, code = acme["id"], acme["code"]
    token = pbx._current_tenant.set(tid)
    try:
        pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35315557777", target="201"))
    finally:
        pbx._current_tenant.reset(token)
    t = pbx.tenant_names(pbx.get_db(), tid)
    os.makedirs(t.rec_dir, exist_ok=True)
    open(os.path.join(t.rec_dir, "x.ulaw"), "wb").write(b"\xff")
    main_before = ext_row(world, 1, "201")
    import billing
    bconn = billing.get_db()
    billing.ensure_tenant_customers(bconn)
    bconn.commit()

    def delete(confirm):
        return op.request("DELETE", f"/api/pbx/tenants/{tid}", json={"confirm": confirm})
    assert delete(code).status_code == 409  # still active
    assert op.put(f"/api/pbx/tenants/{tid}", json={"name": "Acme", "status": "suspended"}).status_code == 200
    r = delete(code)
    assert r.status_code == 409 and "35315557777" in r.json()["detail"]  # its number would reach the main tenant
    conn = pbx.get_db()
    conn.execute("DELETE FROM inbound_routes WHERE tenant_id = ?", (tid,))
    conn.commit()
    assert delete("9999").status_code == 400  # the wrong code
    assert op.request("DELETE", "/api/pbx/tenants/1", json={"confirm": "1001"}).status_code == 400  # never the main one
    r = delete(code)
    assert r.status_code == 200, r.text
    # its phone system is gone; the main tenant's untouched
    assert conn.execute("SELECT COUNT(*) FROM extensions WHERE tenant_id = ?", (tid,)).fetchone()[0] == 0
    assert ext_row(world, 1, "201") == main_before
    assert all(x["id"] != tid for x in op.get("/api/pbx/tenants").json()["tenants"])
    dial = open(os.path.join(pbx.GENERATED_DIR, "extensions_portal.conf")).read()
    assert f"[t{code}-internal]" not in dial and "[portal-call-ext]" in dial
    assert f"[{code}201]" not in open(pbx.EXT_CONF).read()
    # its files kept aside
    assert not os.path.exists(t.rec_dir) and r.json()["files"]["recordings"].startswith(os.path.join(pbx.REC_DIR, "deleted"))
    # its row stays: its code isn't used again
    assert conn.execute("SELECT status FROM tenants WHERE id = ?", (tid,)).fetchone()[0] == "deleted"
    new = op.post("/api/pbx/tenants", json={"ext_digits": "3", "name": "Next", "outbound_cid": "015558000"}).json()
    assert int(new["code"]) > int(code)
    # billing: its account closed, not deleted
    c = billing.get_db().execute("SELECT closed_at FROM customers WHERE tenant_id = ?", (tid,)).fetchone()
    assert c["closed_at"]
    # the portal: gwphones says it's gone, so its people go; the main tenant's stay
    from gwportal import db, gone
    assert gone.purge_gone_tenants() == {code: 3}
    with db.connect() as pconn:
        left = {r["email"] for r in pconn.execute("SELECT email FROM users")}
    assert left == {"zoe@main.example"}
    assert gone.purge_gone_tenants() == {}


def test_portal_keeps_people_when_gwphones_is_just_unreachable(world, monkeypatch):
    from gwportal import auth, gone, gw

    def down(*a, **k):
        raise auth.Refused(gw.UNAVAILABLE, 503)
    monkeypatch.setattr(gw, "call", down)
    assert gone.purge_gone_tenants() == {}


def test_purge_after_thirty_days(env, tmp_path):
    import pbx
    old = os.path.join(pbx.REC_DIR, "deleted", "t1005-" + (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y%m%d"))
    new = os.path.join(pbx.REC_DIR, "deleted", "t1006-" + datetime.now(timezone.utc).strftime("%Y%m%d"))
    other = os.path.join(pbx.REC_DIR, "deleted", "keep-me")
    for d in (old, new, other):
        os.makedirs(d)
    assert pbx.purge_deleted() == [old]
    assert os.path.isdir(new) and os.path.isdir(other)
