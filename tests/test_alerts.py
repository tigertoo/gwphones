"""Alert calls for the pet tracker (alerts.py)."""
import io
import math
import os
import struct
import wave

import pytest

LOCAL = {"X-Alert-Token": "alert-secret"}


def _wav(rate=22050, seconds=0.5):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(rate * seconds))))
    return buf.getvalue()


def local_client(app):
    """A client calling from 127.0.0.1, as Asterisk and the pet tracker do."""
    from fastapi.testclient import TestClient

    async def from_here(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=("127.0.0.1", 50000))
        await app(scope, receive, send)
    return TestClient(from_here)


@pytest.fixture
def alerts(env, monkeypatch):
    monkeypatch.setenv("PBX_ALERT_TOKEN", "alert-secret")
    monkeypatch.setenv("PBX_ALERT_EXT", "201")
    from fastapi.testclient import TestClient
    import main
    import alerts as mod
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    assert op.post("/api/pbx/extensions", json={"ext": "201", "name": "Greg"}).status_code == 200
    sent = []
    monkeypatch.setattr(mod, "speak", lambda text: (sent.append(("speak", text)), _wav())[1])
    monkeypatch.setattr(mod, "asterisk_cli", lambda cmd: (sent.append(("cli", cmd)), "")[1])
    local = local_client(main.app)  # the test client's own address isn't 127.0.0.1
    return mod, local, op, sent


def test_say_call_speaks_and_rings_the_mobile(alerts):
    mod, c, op, sent = alerts
    r = c.post("/alert/call", headers=LOCAL, json={"number": "+353 85 123 4567", "kind": "say",
                                                    "text": "Hello. Sydney's tag has just been scanned."})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert ("speak", "Hello. Sydney's tag has just been scanned.") in sent
    assert ("cli", f"channel originate Local/2010851234567@portal-c2c-dial/n extension {cid}@portal-alert-say") in sent
    audio = open(mod.alert_path(cid), "rb").read()
    assert abs(len(audio) - 4000) < 10  # half a second at 8 kHz


def test_ask_call_records_a_press(alerts):
    mod, c, op, sent = alerts
    cid = c.post("/alert/call", headers=LOCAL, json={"number": "0851234567", "kind": "ask",
                                                      "text": "Press 1 to get alerts for Sydney."}).json()["id"]
    assert c.get(f"/alert/call/{cid}", headers=LOCAL).json()["pressed"] is False
    import pbx
    with pbx.get_db() as conn:
        token = pbx._lcr_token(conn)
    assert c.get(f"/alert/pressed?id={cid}", headers={"X-LCR-Token": "wrong"}).status_code == 403
    assert c.get(f"/alert/pressed?id={cid}", headers={"X-LCR-Token": token}).text == "OK"
    assert c.get(f"/alert/call/{cid}", headers=LOCAL).json()["pressed"] is True


def test_only_from_this_machine_with_the_token(alerts):
    mod, c, op, sent = alerts
    body = {"number": "0851234567", "text": "Hello there"}
    assert c.post("/alert/call", json=body).status_code == 403
    assert c.post("/alert/call", headers={"X-Alert-Token": "nope"}, json=body).status_code == 403
    assert c.post("/alert/call", headers={**LOCAL, "X-Real-IP": "1.2.3.4"}, json=body).status_code == 403
    assert op.post("/alert/call", headers=LOCAL, json=body).status_code == 403  # not 127.0.0.1


def test_mobiles_only_and_never_emergency(alerts):
    mod, c, op, sent = alerts
    for n in ("112", "999", "0612345678", "1800123456", "00441234567890", "15990000"):
        assert c.post("/alert/call", headers=LOCAL, json={"number": n, "text": "Hello there"}).status_code == 400, n
    assert not [s for s in sent if s[0] == "cli"]


def test_calls_are_limited(alerts):
    mod, c, op, sent = alerts
    codes = [c.post("/alert/call", headers=LOCAL, json={"number": "0851234567", "text": "Hello there"}).status_code
             for _ in range(8)]
    assert codes.count(200) == mod.PER_NUMBER_HOUR and codes[-1] == 429


def test_off_without_settings(env, monkeypatch):
    monkeypatch.delenv("PBX_ALERT_TOKEN", raising=False)
    import main
    c = local_client(main.app)
    assert c.post("/alert/call", headers=LOCAL, json={"number": "0851234567", "text": "Hi there"}).status_code == 503


def test_text_is_cleaned(alerts):
    mod, c, op, sent = alerts
    c.post("/alert/call", headers=LOCAL, json={"number": "0851234567", "text": "Rex's <b>tag</b> ${EXTEN} scanned"})
    said = [s[1] for s in sent if s[0] == "speak"][-1]
    assert "<" not in said and "$" not in said and "{" not in said and "Rex's" in said


def test_mulaw_conversion():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
    from alerts import _ulaw_byte
    assert _ulaw_byte(0) == 0xFF and _ulaw_byte(-1) == 0x7F  # silence, either side
    assert _ulaw_byte(32767) == 0x80 and _ulaw_byte(-32768) == 0x00


def test_dialplan_has_the_alert_contexts(alerts):
    mod, c, op, sent = alerts
    import pbx
    with pbx.get_db() as conn:
        plan = pbx.build_portal_dialplan(conn)
    assert "[portal-alert-say]" in plan and "[portal-alert-ask]" in plan
    assert "Read(KEY," in plan and "/alert/pressed?id=${EXTEN}" in plan
    # one digit or more: call 1 must match (_X. needs two)
    assert plan.count("exten => _X!,1,Wait(1)") == 2 and "exten => _X.,1,Wait(1)" not in plan


def test_from_a_tenant_of_its_own(env, monkeypatch):
    """Best: a tenant that only makes alert calls, with its own caller ID and statement."""
    from fastapi.testclient import TestClient
    import main
    op = TestClient(main.app)
    op.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    r = op.post("/api/pbx/tenants", json={"name": "Pet tracker", "outbound_cid": "015557000"})
    tid, code = r.json()["id"], r.json()["code"]
    op.headers["X-PBX-Tenant"] = str(tid)
    op.cookies.set("pbx_tenant", str(tid))
    assert op.post("/api/pbx/extensions", json={"ext": "2000", "name": "Alerts"}).status_code == 200
    import alerts as mod
    monkeypatch.setattr(mod, "ALERT_TOKEN", "alert-secret")
    monkeypatch.setattr(mod, "ALERT_TENANT", code)
    monkeypatch.setattr(mod, "ALERT_EXT", "2000")
    monkeypatch.setattr(mod, "speak", lambda text: _wav())
    cmds = []
    monkeypatch.setattr(mod, "asterisk_cli", lambda cmd: (cmds.append(cmd), "")[1])
    c = local_client(main.app)
    r = c.post("/alert/call", headers=LOCAL, json={"number": "0851234567", "text": "Hello there"})
    assert r.status_code == 200, r.text
    import pbx
    with pbx.get_db() as conn:
        dial = pbx.tenant_names(conn, tid).c2c_dial
    assert dial != "portal-c2c-dial" and cmds[-1].startswith(f"channel originate Local/20000851234567@{dial}/n ")
    # an extension of another tenant doesn't do
    monkeypatch.setattr(mod, "ALERT_EXT", "201")
    assert c.post("/alert/call", headers=LOCAL, json={"number": "0851234567", "text": "Hello"}).status_code == 503
    monkeypatch.setattr(mod, "ALERT_TENANT", "9999")
    assert c.post("/alert/call", headers=LOCAL, json={"number": "0851234567", "text": "Hello"}).status_code == 503
