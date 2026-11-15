"""The firmware library: uploads, a target per model, static.firmware.url
only for a phone on something else, and the files only for phones with
valid credentials."""
import os

import pytest
from fastapi.testclient import TestClient

from test_desk_phones import MAC, MAC2, basic, phones  # noqa: F401
from test_portal_caller import PORTAL, two  # noqa: F401

ROM = b"\x00YEALINK-ROM" + os.urandom(1024 * 1024)
OLD = f"Yealink SIP-T46G 28.80.0.136 {MAC}"
NEW = f"Yealink SIP-T46G 28.83.0.120 {MAC}"


def upload(op, model="T46G", version="28.83.0.120", body=ROM):
    return op.post(f"/api/pbx/desk-firmware?model={model}&version={version}", content=body,
                   headers={"Content-Type": "application/octet-stream"})


@pytest.fixture
def lib(phones, tmp_path, monkeypatch):
    import deskphones
    monkeypatch.setattr(deskphones, "FIRMWARE_DIR", str(tmp_path / "firmware"))
    main, pbx, op, acme_op, acme, code, pw = phones
    r = upload(op)
    assert r.status_code == 200, r.text
    fw = op.get("/api/pbx/desk-firmware").json()["files"][0]
    return main, pbx, op, pw, fw, deskphones


def fetch(main, mac, pw, agent):
    return TestClient(main.app).get(f"/prov/yealink/{mac}.cfg", headers={**basic(mac, pw), "User-Agent": agent})


def test_upload_checks(lib):
    main, pbx, op, pw, fw, deskphones = lib
    assert fw["model"] == "T46G" and fw["version"] == "28.83.0.120" and fw["name"] == "T46G-28.83.0.120.rom"
    assert fw["size"] == len(ROM) and os.path.isfile(os.path.join(deskphones.FIRMWARE_DIR, fw["name"]))
    assert upload(op).status_code == 409  # already there
    assert upload(op, model="X99").status_code == 400
    assert upload(op, version="../../etc").status_code == 400
    assert upload(op, version="28.80.0.136", body=b"PK\x03\x04" + ROM).status_code == 400  # a zip
    assert upload(op, version="28.80.0.136", body=b"<!DOCTYPE html>" + ROM).status_code == 400
    assert upload(op, version="28.80.0.136", body=b"x" * 1000).status_code == 400  # too small
    assert [n for n in os.listdir(deskphones.FIRMWARE_DIR)] == [fw["name"]]  # nothing left behind


def test_too_big(lib, monkeypatch):
    main, pbx, op, pw, fw, deskphones = lib
    monkeypatch.setattr(deskphones, "FIRMWARE_MAX_BYTES", 2 * 1024 * 1024)
    assert upload(op, version="28.80.0.136", body=ROM * 3).status_code == 413
    assert len(os.listdir(deskphones.FIRMWARE_DIR)) == 1


def test_only_a_phone_on_something_else_is_told(lib):
    main, pbx, op, pw, fw, deskphones = lib
    assert "static.firmware.url" not in fetch(main, MAC, pw[MAC], OLD).text  # no target yet
    assert op.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": fw["id"]}).status_code == 200
    cfg = fetch(main, MAC, pw[MAC], OLD).text
    assert f"static.firmware.url = https://{MAC}:{pw[MAC]}@pbx.example/prov/yealink/firmware/T46G-28.83.0.120.rom" in cfg
    assert "static.firmware.url" not in fetch(main, MAC, pw[MAC], NEW).text  # already on it
    assert "static.firmware.url" not in fetch(main, MAC, pw[MAC], f"Yealink SIP-T41P 36.80.0.1 {MAC}").text  # another model
    assert "static.firmware.url" not in fetch(main, MAC, pw[MAC], "").text  # doesn't say
    # the phone lists show what it runs
    fetch(main, MAC, pw[MAC], OLD)
    p = op.get("/api/pbx/desk-phones").json()["phones"][0]
    assert p["firmware"] == "28.80.0.136" and p["upgrade_to"] == "28.83.0.120"
    # the checking copy hides the credentials
    view = op.get(f"/api/pbx/desk-phones/{MAC}/config.cfg").text
    assert "static.firmware.url = https://********@pbx.example/" in view and pw[MAC] not in view
    assert op.get("/api/pbx/desk-firmware").json()["fleet"] == {"T46G": {"28.80.0.136": 1}}
    # none again: left as they are
    assert op.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": None}).status_code == 200
    assert "static.firmware.url" not in fetch(main, MAC, pw[MAC], OLD).text


def test_target_must_match_model(lib):
    main, pbx, op, pw, fw, deskphones = lib
    assert op.put("/api/pbx/desk-firmware/target/T41P", json={"firmware_id": fw["id"]}).status_code == 400


def test_download_needs_a_phones_credentials(lib):
    main, pbx, op, pw, fw, deskphones = lib
    c = TestClient(main.app)
    url = f"/prov/yealink/firmware/{fw['name']}"
    assert c.get(url).status_code == 401
    assert c.get(url, headers=basic(MAC, "wrong")).status_code == 401
    r = c.get(url, headers=basic(MAC2, pw[MAC2]))  # any phone of ours, any tenant
    assert r.status_code == 200 and r.content == ROM
    assert c.get("/prov/yealink/firmware/T46G-1.2.3.rom", headers=basic(MAC, pw[MAC])).status_code == 404


def test_delete(lib):
    main, pbx, op, pw, fw, deskphones = lib
    op.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": fw["id"]})
    assert op.delete(f"/api/pbx/desk-firmware/{fw['id']}").status_code == 409  # the target
    op.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": None})
    assert op.delete(f"/api/pbx/desk-firmware/{fw['id']}").status_code == 200
    assert os.listdir(deskphones.FIRMWARE_DIR) == [] and op.get("/api/pbx/desk-firmware").json()["files"] == []


def test_not_for_the_portal(lib, phones):
    main, pbx, op, acme_op, acme, code, pw = phones
    portal = TestClient(main.app)
    portal.headers.update({"X-Portal-Secret": PORTAL, "X-Portal-Tenant": code})
    assert portal.get("/api/pbx/desk-firmware").status_code == 403
    assert portal.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": None}).status_code == 403


def test_wrong_label_stops_after_three_tries(lib):
    """A file labelled 28.83.0.121 that installs as .120: three tries, then
    left alone (and shown stuck) until the target is chosen again."""
    main, pbx, op, pw, fw, deskphones = lib
    assert upload(op, version="28.83.0.121", body=ROM + b"x").status_code == 200
    f121 = next(f for f in op.get("/api/pbx/desk-firmware").json()["files"] if f["version"] == "28.83.0.121")
    op.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": f121["id"]})
    got = ["static.firmware.url" in fetch(main, MAC, pw[MAC], NEW).text for _ in range(5)]
    assert got == [True, True, True, False, False]
    p = op.get("/api/pbx/desk-phones").json()["phones"][0]
    assert p["upgrade_to"] == "28.83.0.121" and p["upgrade_stuck"]
    # choosing it again: tries again
    op.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": f121["id"]})
    assert "static.firmware.url" in fetch(main, MAC, pw[MAC], NEW).text
    # reporting the target clears it
    fetch(main, MAC, pw[MAC], f"Yealink SIP-T46G 28.83.0.121 {MAC}")
    assert pbx.get_db().execute("SELECT COUNT(*) FROM desk_firmware_attempts").fetchone()[0] == 0
    assert not op.get("/api/pbx/desk-phones").json()["phones"][0]["upgrade_to"]


def test_view_doesnt_count(lib):
    main, pbx, op, pw, fw, deskphones = lib
    upload(op, version="28.83.0.121", body=ROM + b"y")
    f121 = next(f for f in op.get("/api/pbx/desk-firmware").json()["files"] if f["version"] == "28.83.0.121")
    op.put("/api/pbx/desk-firmware/target/T46G", json={"firmware_id": f121["id"]})
    fetch(main, MAC, pw[MAC], NEW)
    for _ in range(5):
        assert "static.firmware.url" in op.get(f"/api/pbx/desk-phones/{MAC}/config.cfg").text
    assert pbx.get_db().execute("SELECT count FROM desk_firmware_attempts").fetchone()[0] == 1
