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
        assert "Bridge(${peer},x)" in b and '"${c}" = "${CHANNEL}"' in b  # never its own channel
        assert "ExecIf" not in b
