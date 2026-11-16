"""Saves that aren't applied to Asterisk: kept with their tenant and who
made them, retried with a back-off, the operator emailed once after
ALERT_AFTER_MINUTES and again when they're live."""
from datetime import timedelta

import pytest

from test_portal_phone import PW, world  # noqa: F401  (the two-tenant world)


@pytest.fixture
def broken(world, monkeypatch):
    pbx = world["pbx"]
    state = {"ok": False, "applies": 0}

    def node(name, files):
        state["applies"] += 1
        return (True, []) if state["ok"] else (False, ["Asterisk didn't reload: connection refused"])
    monkeypatch.setattr(pbx, "apply_to_node", node)
    sent = []
    monkeypatch.setattr(pbx, "_graph_ready", lambda: True)
    monkeypatch.setattr(pbx, "_graph_send", lambda to, subject, body, images: sent.append((to, subject, body)))
    monkeypatch.setattr(pbx, "ALERT_EMAIL", "ops@example.ie")
    now = {"t": pbx._utcnow()}
    monkeypatch.setattr(pbx, "_utcnow", lambda: now["t"])
    pbx._retry.update(next=None, step=0)
    pbx._alert.update(sent=None)
    with pbx.get_db() as conn:
        conn.execute("DELETE FROM apply_pending")
        conn.commit()
    return pbx, state, sent, now


def tick(pbx):
    due = pbx.retry_unapplied()
    if due:
        pbx.send_apply_alert(due)
    return due


def test_portal_save_is_kept_with_tenant_and_who(world, broken):
    pbx, state, sent, now = broken
    admin = world["client"]("admin")
    r = admin.put("/api/extensions/202", json={"name": "Reception"})
    assert r.status_code == 200 and r.json().get("pending")
    u = world["op"].get("/api/pbx/status").json()["unapplied"]
    assert u["count"] == 1 and u["changes"][0]["tenant"] == "Acme" and u["changes"][0]["by"] == "admin@acme.example"
    assert "connection refused" in u["changes"][0]["messages"]


def test_retry_backs_off_then_applies(world, broken):
    pbx, state, sent, now = broken
    world["op"].post("/api/pbx/apply")
    assert tick(pbx) is None and state["applies"] == 2  # retried at once
    assert tick(pbx) is None and state["applies"] == 2  # not before a minute
    now["t"] += timedelta(minutes=1)
    tick(pbx)
    assert state["applies"] == 3
    now["t"] += timedelta(minutes=1)
    tick(pbx)
    assert state["applies"] == 3  # now two minutes
    now["t"] += timedelta(minutes=1)
    state["ok"] = True
    tick(pbx)
    assert state["applies"] == 4 and world["op"].get("/api/pbx/status").json()["unapplied"]["count"] == 0


def test_alert_once_then_resolved(world, broken):
    pbx, state, sent, now = broken
    world["client"]("admin").put("/api/extensions/202", json={"name": "Reception"})
    tick(pbx)
    assert sent == []  # not yet
    for _ in range(10):
        now["t"] += timedelta(minutes=1)
        tick(pbx)
    assert len(sent) == 1 and sent[0][0] == "ops@example.ie" and "not applied" in sent[0][1]
    assert "admin@acme.example" in sent[0][2] and "Acme" in sent[0][2]
    for _ in range(30):  # still broken: no more emails
        now["t"] += timedelta(minutes=1)
        tick(pbx)
    assert len(sent) == 1
    state["ok"] = True
    now["t"] += timedelta(minutes=15)
    tick(pbx)
    assert len(sent) == 2 and "applied again" in sent[1][1]
    now["t"] += timedelta(minutes=15)
    tick(pbx)
    assert len(sent) == 2


def test_a_save_that_works_clears_them(world, broken):
    pbx, state, sent, now = broken
    world["op"].post("/api/pbx/apply")
    state["ok"] = True
    world["op"].post("/api/pbx/apply")
    assert world["op"].get("/api/pbx/status").json()["unapplied"]["count"] == 0


def test_retries_add_no_rows(world, broken):
    pbx, state, sent, now = broken
    world["op"].post("/api/pbx/apply")
    for _ in range(5):
        now["t"] += timedelta(minutes=15)
        tick(pbx)
    with pbx.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM apply_pending").fetchone()[0] == 1


def test_no_alert_address_logs_only(world, broken, monkeypatch):
    pbx, state, sent, now = broken
    monkeypatch.setattr(pbx, "ALERT_EMAIL", "")
    world["op"].post("/api/pbx/apply")
    now["t"] += timedelta(minutes=10)
    tick(pbx)
    assert sent == []


def test_portal_status_doesnt_show_it(world, broken):
    admin = world["client"]("admin")
    admin.put("/api/extensions/202", json={"name": "Reception"})
    s = admin.get("/api/status").json()
    assert "unapplied" not in s and "last_apply" not in s
