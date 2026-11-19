"""One person per extension in Staff: inviting, moving or switching
someone back on refuses an extension an active person already has;
any older duplicates are flagged."""
from test_portal_phone import world  # noqa: F401  (the two-tenant world: ann on 201, bob on 202)


def uid(email):
    from gwportal import db
    with db.connect() as conn:
        return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def test_invite_and_move_refuse_a_taken_extension(world):
    admin = world["client"]("admin")
    r = admin.post("/api/users", json={"email": "cara@acme.example", "role": "user", "ext": "201"})
    assert r.status_code == 409 and "already" in r.json()["detail"]
    r = admin.put(f"/api/users/{uid('bob@acme.example')}", json={"ext": "201"})
    assert r.status_code == 409
    # ann disabled: 201 is free for bob
    assert admin.put(f"/api/users/{uid('ann@acme.example')}", json={"disabled": True}).status_code == 200
    assert admin.put(f"/api/users/{uid('bob@acme.example')}", json={"ext": "201"}).status_code == 200
    # and ann can't be switched back on while bob has it
    r = admin.put(f"/api/users/{uid('ann@acme.example')}", json={"disabled": False})
    assert r.status_code == 409


def test_older_duplicates_flagged(world):
    from gwportal import db
    with db.connect() as conn:
        conn.execute("UPDATE users SET ext = '201', ext_created = (SELECT ext_created FROM users WHERE email = "
                     "'ann@acme.example') WHERE email = 'bob@acme.example'")
    users = {u["email"]: u for u in world["client"]("admin").get("/api/users").json()["users"]}
    assert users["ann@acme.example"]["shares_ext"] and users["bob@acme.example"]["shares_ext"]
    assert not users["admin@acme.example"]["shares_ext"]
