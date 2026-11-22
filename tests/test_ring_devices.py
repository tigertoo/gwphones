"""A desk phone and the apps on one extension: up to MAX_DEVICES at once,
every device rung (not just the first), by the extension's ring mode;
the portal may set it for the extension's own user or an admin."""
from test_portal_phone import world  # noqa: F401  (the two-tenant world)


def conf(world, name):
    import os
    return open(os.path.join(world["pbx"].GENERATED_DIR, name)).read()


def test_several_devices_and_every_one_rung(world):
    pjsip = conf(world, "pjsip_extensions.conf")
    assert "max_contacts=4" in pjsip
    dial = conf(world, "extensions_portal.conf")
    assert "[sub-ring-ext]" in dial and "PJSIP_DIAL_CONTACTS(${ARG1})" in dial
    # an extension's call, click to call and ring groups all go through it; none dials PJSIP/<login> alone
    assert "Gosub(sub-ring-ext,s,1(${EXTEN},30,${RINGMODE_${EXTEN}},rec))" in dial
    import re
    assert not re.search(r"Dial\(PJSIP/(1002)?(\$\{EXTEN[^}]*\}|2\d\d)[,)]", dial)
    # never Dial's c option: it would hide real missed calls (abandoned, rung out)
    assert "Set(LOCAL(o)=U(portal-rec))" in dial
    import re as _re
    assert not any(_re.search(r"Dial\([^)]*,[^,)]*,[^)]*c\)", ln) and "portal-rec)c" in ln for ln in dial.splitlines())
    # no ExecIf around a contact's address: it splits on ":"
    assert not any("ExecIf" in ln and ("PJSIP_DIAL_CONTACTS" in ln or "${d}" in ln) for ln in dial.splitlines())


def test_ring_mode_from_the_portal(world):
    acme = world["acme"]["id"]
    ann = world["client"]("ann")
    r = ann.put("/api/extensions/201/ring-mode", json={"mode": "desk_first"})
    assert r.status_code == 200 and r.json()["ok"]
    assert ann.get("/api/extensions").json()["extensions"][0]["ring_mode"] == "desk_first"
    assert f"RINGMODE_{world['acme']['code']}201=desk_first" in conf(world, "extensions_portal.conf")
    assert ann.put("/api/extensions/202/ring-mode", json={"mode": "backup"}).status_code == 404  # not hers
    assert ann.put("/api/extensions/201/ring-mode", json={"mode": "loud"}).status_code == 400
    admin = world["client"]("admin")
    assert admin.put("/api/extensions/202/ring-mode", json={"mode": "backup"}).status_code == 200
    # back to all: no global for it
    assert ann.put("/api/extensions/201/ring-mode", json={"mode": "all"}).status_code == 200
    assert f"RINGMODE_{world['acme']['code']}201=" not in conf(world, "extensions_portal.conf")
    assert world["pbx"].get_db().execute("SELECT ring_mode FROM extensions WHERE tenant_id = ? AND ext = '202'",
                                         (acme,)).fetchone()[0] == "backup"


def test_status_shows_the_best_of_several_devices(env):
    import pbx
    out = """  Contact:  201/sip:201@10.0.0.5:5060;ob                     a1b2c3d4e5 Unavail         nan
  Contact:  201/sip:201@157.240.1.1:443;transport=tls          f6e7d8c9b0 Avail          31.2
  Contact:  202/sip:202@10.0.0.9:5061;transport=tls            0a9b8c7d6e Avail          12.0
"""
    c = pbx.parse_contacts(out)
    assert c["201"]["status"] == "Avail" and c["201"]["devices"] == 2 and c["201"]["rtt_ms"] == 31.2
    assert c["202"]["devices"] == 1


def test_call_pull_only_takes_the_extensions_own_call(world):
    dial = conf(world, "extensions_portal.conf")
    code = world["acme"]["code"]
    blocks = dial.split("exten => *11,1,Answer()")
    assert len(blocks) == 3  # once per tenant
    main_block, acme_block = blocks[1].split("\nexten => ")[0], blocks[2].split("\nexten => ")[0]
    # each looks only at its own extension's channels, from its endpoint (PBX_EXT), never the caller ID
    assert "Set(ME=${PBX_EXT})" in main_block and "CHANNELS(^PJSIP/${ME}-)" in main_block
    assert f"CHANNELS(^PJSIP/{code}${{ME}}-)" in acme_block
    for b in (main_block, acme_block):
        assert "Bridge(${PULL_PEER},x)" in b and '"${PULL_C}" = "${CHANNEL}"' in b  # never its own channel
        assert "ExecIf" not in b and "LOCAL(" not in b  # LOCAL() works only in a Gosub


CONTACTS = """  Contact:  1002201/sips:1002201@194.125.12.215:61209;tra 9f3c2a1b7e Avail         41.9
  Contact:  1002201/sips:1002201@165.227.210.221:15405;tra 1dc4fb0daf Avail        97.693
  Contact:  1002202/sip:1002202@10.0.0.9:5060;ob             0a9b8c7d6e Unavail         nan
  Contact:  201/sips:201@167.99.119.244:24653;transport=TL 4556be3b39 Avail        91.842
"""
REGISTRAR = """/registrar/contact/1002201;@9f3c2a1b7e5d4c3b2a1f0e9d8c7b6a59: {"user_agent":"Yealink SIP-T46G 28.83.0.120","uri":"sips:1002201@194.125.12.215:61209;transport=TLS;rinstance=8727;x-ast-orig-host=192.168.178.40:49281","via_addr":"194.125.12.215"}
/registrar/contact/1002201;@1dc4fb0daf00000000000000000000aa: {"user_agent":"Acrobits SIPIS","uri":"sips:1002201@165.227.210.221:15405;transport=TLS;x-ast-orig-host=10.65.4.2:15405"}
/registrar/contact/1002202;@0a9b8c7d6e00000000000000000000bb: {"user_agent":"Linphone-Desktop/5.2 (belle-sip/5.2)","uri":"sip:1002202@10.0.0.9:5060"}
3 results found.
"""


def test_each_device_listed_with_what_it_is(env, monkeypatch):
    import pbx
    monkeypatch.setattr(pbx, "asterisk_cli", lambda cmd, timeout=8: REGISTRAR if cmd.startswith("database") else CONTACTS)
    t = pbx.TenantNames(2, "1002")
    d = pbx.extension_devices(t)
    assert [x["label"] for x in d["201"]] == ["Yealink T46G desk phone", "Groundwire app (through its push server)"]
    assert d["201"][0]["kind"] == "desk" and d["201"][0]["online"] and d["201"][0]["address"] == "194.125.12.215"
    assert d["202"] == [{**d["202"][0], "label": "Linphone app", "online": False}]
    assert "201" not in pbx.extension_devices(pbx.MAIN_NAMES) or len(pbx.extension_devices(pbx.MAIN_NAMES)["201"]) == 1
    assert pbx.device_label("Cisco-CP-6851-3PCC/12.0.1 (00562b043615)") == ("Cisco 6851 desk phone", "desk")
    assert pbx.device_label("Panasonic_KX-HDV130/16.009 (0080f0b5b001)") == ("Panasonic HDV130 desk phone", "desk")
    assert pbx.device_label("Groundwire/26.1.21 (build 2549327; iOS 27.2.1)")[0] == "Groundwire app"


def test_desk_phone_office_address_and_mac(world, monkeypatch):
    pbx = world["pbx"]
    monkeypatch.setattr(pbx, "asterisk_cli", lambda cmd, timeout=8: REGISTRAR if cmd.startswith("database")
                        else (CONTACTS if "contacts" in cmd else "Asterisk 20.6.0"))
    conn = pbx.get_db()
    conn.execute("INSERT INTO desk_phones (mac, ext, prov_pass, admin_pass, created_at, tenant_id) "
                 "VALUES ('001565b04147', '201', 'p', 'a', '2026-11-22', ?)", (world["acme"]["id"],))
    conn.commit()
    d = pbx.extension_devices(pbx.TenantNames(world["acme"]["id"], world["acme"]["code"]))["201"]
    desk = d[0]
    assert desk["lan_ip"] == "192.168.178.40" and desk["mac"] == "00:15:65:B0:41:47"
    assert d[1]["mac"] == "" and d[1]["lan_ip"] == "" and d[1]["address"] is None  # an app: its push server's, so none
    world["phone"]._status.clear()
    admin = world["client"]("admin").get("/api/status").json()["phones"]["201"]["devices"][0]
    assert admin["mac"] == "00:15:65:B0:41:47" and admin["lan_ip"] == "192.168.178.40"
    world["phone"]._status.clear()
    ann = world["client"]("ann").get("/api/status").json()["phones"]["201"]["devices"][0]
    assert "mac" not in ann and "lan_ip" not in ann  # staff: the name and whether it's online only
