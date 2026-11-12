"""Voicemail without the PIN from an extension's own phones (*97, per
extension), and a PIN its user chooses."""
import pytest

from test_portal_caller import two  # noqa: F401  (main tenant and Acme, both with a 201)


def ext(pbx, tid, number):
    return dict(pbx.get_db().execute("SELECT * FROM extensions WHERE tenant_id = ? AND ext = ?", (tid, number)).fetchone())


def test_no_pin_from_own_phones(two):
    main, pbx, op, portal, acme = two
    before = open(pbx.PORTAL_DIALPLAN).read()
    assert "VM_NOPIN" not in before + open(pbx.EXT_CONF).read()  # nothing until someone chooses it
    assert portal.put("/api/pbx/extensions/201/voicemail-nopin", json={"on": True}).status_code == 200
    assert ext(pbx, acme, "201")["vm_nopin"] == 1 and ext(pbx, 1, "201")["vm_nopin"] == 0
    code = pbx.tenant_names(pbx.get_db(), acme).code
    conf = open(pbx.EXT_CONF).read()
    assert "set_var=VM_NOPIN=1" in conf.split(f"[{code}201](endpoint-ext)")[1].split("\n[")[0]
    assert "VM_NOPIN" not in conf.split("[201](endpoint-ext)")[1].split("\n[")[0]  # the main tenant's 201
    gen = open(pbx.PORTAL_DIALPLAN).read()
    acme_ctx = gen.split(f"[t{code}-internal]")[1].split("\n[")[0]
    main_ctx = gen.split("[portal-internal]")[1].split("\n[")[0]
    assert 'exten => *97,1,GotoIf($["${VM_NOPIN}" = "1"]?nopin)' in acme_ctx
    assert f"n(nopin),VoiceMailMain(${{PBX_EXT}}@t{code},s)" in acme_ctx
    assert "*97,1,VoiceMailMain(${PBX_EXT}@portal)" in main_ctx and "nopin" not in main_ctx
    assert "*98,1,VoiceMailMain(@" in acme_ctx  # *98 (any mailbox) still always asks
    assert portal.put("/api/pbx/extensions/201/voicemail-nopin", json={"on": False}).status_code == 200
    assert "VM_NOPIN" not in open(pbx.PORTAL_DIALPLAN).read() + open(pbx.EXT_CONF).read()


def test_choose_own_pin(two):
    main, pbx, op, portal, acme = two
    theirs = ext(pbx, 1, "201")["vm_pin"]
    assert portal.put("/api/pbx/extensions/201/voicemail-pin", json={"pin": "482916"}).status_code == 200
    assert ext(pbx, acme, "201")["vm_pin"] == "482916" and ext(pbx, 1, "201")["vm_pin"] == theirs
    code = pbx.tenant_names(pbx.get_db(), acme).code
    vm = open(pbx.VOICEMAIL_CONF).read().split(f"[t{code}]")[1]
    assert "201 => 482916," in vm
    assert "482916" not in open(pbx.CHANGES_LOG_PATH).read()  # never written to the log
    assert portal.put("/api/pbx/extensions/203/voicemail-pin", json={"pin": "482916"}).status_code == 404  # not Acme's


@pytest.mark.parametrize("pin", ["123", "123456789", "12a4", "1111", "1234", "4321", "0000", "345678", "98765",
                                 "92019", ""])
def test_weak_or_malformed_pins_refused(two, pin):
    main, pbx, op, portal, acme = two
    before = ext(pbx, acme, "201")["vm_pin"]
    assert portal.put("/api/pbx/extensions/201/voicemail-pin", json={"pin": pin}).status_code == 400, pin
    assert ext(pbx, acme, "201")["vm_pin"] == before


# PINs changed from a phone's voicemail menu (asterisk/vm-pin.sh, Asterisk's
# externpasscheck and externpass)

import os
import subprocess

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "asterisk", "vm-pin.sh")
PINS = ["482916", "5820", "73914826", "123", "123456789", "12a4", "", "1111", "000000", "1234", "4321", "345678",
        "98765", "0987", "90123", "9201", "2019", "62014"]


def script(*args, spool=None):
    env = {**os.environ, **({"VM_PINS": spool} if spool else {})}
    return subprocess.run(["sh", SCRIPT, *args], capture_output=True, text=True, env=env)


@pytest.mark.parametrize("pin", PINS)
def test_phone_check_matches_the_portal(env, pin):
    """The phone menu refuses exactly what the portal refuses."""
    import pbx
    try:
        pbx._clean_pin(pin, "201")
        portal_ok = True
    except Exception:
        portal_ok = False
    assert script("check", "201", "portal", "0000", pin).stdout.strip() == ("VALID" if portal_ok else "INVALID"), pin


def test_phone_pin_reaches_gwphones(two, tmp_path, monkeypatch):
    main, pbx, op, portal, acme = two
    tmp_path = tmp_path / "vm-pins"
    tmp_path.mkdir()
    monkeypatch.setattr(pbx, "VM_PINS", str(tmp_path))
    code = pbx.tenant_names(pbx.get_db(), acme).code
    assert script("set", f"t{code}", "201", "582047", spool=str(tmp_path)).returncode == 0
    assert script("set", f"t{code}", "201", "1234", spool=str(tmp_path)).returncode != 0  # never spooled
    assert script("set", "t;rm", "201", "582047", spool=str(tmp_path)).returncode != 0
    assert len(os.listdir(tmp_path)) == 1
    theirs = ext(pbx, 1, "201")["vm_pin"]
    assert pbx.take_phone_pins() == 1 and os.listdir(tmp_path) == []
    assert ext(pbx, acme, "201")["vm_pin"] == "582047" and ext(pbx, 1, "201")["vm_pin"] == theirs
    assert "201 => 582047," in open(pbx.VOICEMAIL_CONF).read().split(f"[t{code}]")[1]
    log = open(pbx.CHANGES_LOG_PATH).read()
    assert f"Voicemail PIN for extension 201 changed from the phone (tenant {code})" in log and "582047" not in log


def test_phone_pin_files_checked(two, tmp_path, monkeypatch):
    main, pbx, op, portal, acme = two
    tmp_path = tmp_path / "vm-pins"
    tmp_path.mkdir()
    monkeypatch.setattr(pbx, "VM_PINS", str(tmp_path))
    before = {r["ext"]: r["vm_pin"] for r in pbx.get_db().execute("SELECT ext, vm_pin FROM extensions")}
    for i, text in enumerate(["nosuchcontext 201 582047", "portal 299 582047", "portal 201 1111", "garbage",
                              "portal 201 582047 extra"]):
        (tmp_path / f"{i}.pin").write_text(text + "\n")
    (tmp_path / ".in.abc").write_text("portal 201 582047\n")  # still being written: left alone
    assert pbx.take_phone_pins() == 0
    assert os.listdir(tmp_path) == [".in.abc"]
    assert {r["ext"]: r["vm_pin"] for r in pbx.get_db().execute("SELECT ext, vm_pin FROM extensions")} == before
    assert "582047" not in open(pbx.CHANGES_LOG_PATH).read()
