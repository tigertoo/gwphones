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
