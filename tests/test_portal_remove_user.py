"""Tenant admins remove people: everything that lets them sign in goes,
their extension stays, and they can be invited again. Never yourself,
never the last admin, never another tenant's person; staff can't."""
from test_portal_phone import ext_row, world  # noqa: F401  (the two-tenant world)


def uid(email):
    from gwportal import db
    with db.connect() as conn:
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def count(table, user_id):
    from gwportal import db
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)).fetchone()[0]


def test_remove_signs_them_out_and_frees_the_email(world):
    acme = world["acme"]["id"]
    ann = world["client"]("ann")
    assert ann.get("/api/extensions").status_code == 200
    ann_id = uid("ann@acme.example")
    assert count("sessions", ann_id) >= 1
    admin = world["client"]("admin")
    r = admin.delete(f"/api/users/{ann_id}")
    assert r.status_code == 200 and r.json()["ok"]
    assert "ann@acme.example" not in [u["email"] for u in admin.get("/api/users").json()["users"]]
    for table in ("sessions", "links", "recovery_codes", "trusted_devices", "passkeys"):
        assert count(table, ann_id) == 0, table
    assert ann.get("/api/extensions").status_code == 401  # signed out at once
    assert ext_row(world, acme, "201")["name"]  # the extension stays
    # invited again, the same email
    r = admin.post("/api/users", json={"email": "ann@acme.example", "role": "user", "ext": "201"})
    assert r.status_code == 200
    from gwportal import db
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'staff removed' "
                            "AND detail LIKE 'ann@acme.example%'").fetchone()[0] == 1


def test_not_yourself_but_another_admin(world):
    """Never yourself, so an organisation always keeps the admin doing the
    removing: the last-admin check behind it can't be reached from here."""
    admin = world["client"]("admin")
    me = uid("admin@acme.example")
    assert admin.delete(f"/api/users/{me}").status_code == 400
    assert admin.post("/api/users", json={"email": "dan@acme.example", "role": "admin"}).status_code == 200
    assert admin.delete(f"/api/users/{uid('dan@acme.example')}").status_code == 200


def test_staff_and_other_tenants_refused(world):
    zoe = uid("zoe@main.example")
    admin = world["client"]("admin")
    assert admin.delete(f"/api/users/{zoe}").status_code == 404
    ann = world["client"]("ann")
    assert ann.delete(f"/api/users/{uid('bob@acme.example')}").status_code == 403
    assert uid("zoe@main.example") and uid("bob@acme.example")
