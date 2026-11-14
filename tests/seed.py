"""A fixed set of test data, and the config generated from it.

    seed(data_dir)  fills a fresh pbx.db/billing.db (paths from the PBX_*
                    settings, see conftest.env_for) with two tenants'
                    extensions, groups, a menu, hours, numbers, call links
                    and settings.
    build_all()     the generated config and the effective settings, with
                    the run's own folder written as <DATA>.

    python tests/seed.py --update   rewrites tests/golden/ (review the diff)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "golden")
BOOKKEEPING = {"tenant_settings_moved"}  # one-off migration markers, not settings
FILES = ("pjsip_extensions.conf", "extensions_portal.conf", "voicemail_portal.conf", "res_parking_portal.conf",
         "settings.json")


def seed():
    import pbx, ivr, hours, calllinks, recordings, billing
    c = pbx.get_db()
    for name in ("Alex", "Reception", "Sales", "Off"):
        pbx.api_create_extension(pbx.ExtensionCreate(name=name))
    pbx.api_update_extension("204", pbx.ExtensionUpdate(name="Off", enabled=False))
    pbx.api_extension_outbound_cid("202", pbx.OutboundCidRequest(outbound_cid="0871234567"))
    pbx.api_voicemail_email("201", pbx.VoicemailEmailRequest(email="alex@example.com"))
    recordings.api_extension_recording("201", recordings.ExtensionRecording(always=True))
    recordings._store_notice(b"\xff" * 8000)
    pbx.api_create_group(pbx.RingGroupRequest(name="Everyone", members=["201", "202", "203"], voicemail="201"))
    pbx.api_create_group(pbx.RingGroupRequest(name="Hunt", strategy="hunt", members=["202", "203"]))
    ivr.api_create_menu(ivr.MenuRequest(name="Main", options={"1": "600", "2": "vm:202", "0": "hangup"},
                                        fallback="201", direct_dial=True))
    ivr.write_ulaw(pbx.greeting_path("500"), b"\xff" * 8000)
    hours.api_create_hours(hours.HoursRequest(
        name="Office", week={"mon": [["09:00", "13:00"], ["14:00", "17:30"]], "fri": [["09:00", "16:00"]]},
        holidays=["2099-12-25"], open_target="500", closed_target="vm:201"))
    pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35315550000", target="400", label="Main"))
    pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35315550001", target="202"))
    calllinks.api_add_call_link(calllinks.CallLinkRequest(label="Website", target="500"))
    calllinks.api_add_call_link(calllinks.CallLinkRequest(label="Video sales", target="600", video=True))
    s = pbx.get_settings(c)
    pbx.api_update_settings(pbx.SettingsRequest(
        outbound_cid="015550000", max_outbound=6, voxbeam_prefix=s["voxbeam_prefix"],
        trunk_order=["irishvoip", "voxbeam"], trunks_enabled={"irishvoip": True, "voxbeam": True},
        inbound_default="600", routes={"landline": "cheapest", "international": "off"},
        caps={"default": "0.06", "mobile": "0.08"}, video=True))
    # A second tenant, with its own 201 and 202, a number and a limit.
    t = pbx.api_create_tenant(pbx.TenantCreate(name="Acme", outbound_cid="015559999", max_calls=3,
                                               sip_host=""))
    tid = c.execute("SELECT id FROM tenants WHERE name = 'Acme'").fetchone()["id"]
    token = pbx._current_tenant.set(tid)
    try:
        for name in ("Front desk", "Back office"):
            pbx.api_create_extension(pbx.ExtensionCreate(name=name))
        pbx.api_set_inbound_route(pbx.InboundRouteRequest(did="35315559999", target="201"))
    finally:
        pbx._current_tenant.reset(token)
    b = billing.get_db()
    b.execute("INSERT INTO customers (name, created_at) VALUES ('Acme', '2026-11-01')")
    b.commit()
    # What's random by design (passwords, PINs, the route-check token) made
    # fixed, so the generated config is the same on every run.
    c = pbx.get_db()
    c.execute("UPDATE extensions SET password = 'pw-' || tenant_id || '-' || ext, vm_pin = '1' || ext")
    c.execute("UPDATE settings SET value = 'lcr-test-token' WHERE key = 'lcr_token'")
    c.execute("UPDATE settings SET value = 'webguest-test-password' WHERE key = 'webguest_password'")
    c.execute("UPDATE call_links SET token = 'test-link-' || id")
    c.commit()
    pbx.apply_config(c, "seed")
    return t


def build_all():
    import pbx, lcr
    c = pbx.get_db()
    data = os.path.dirname(pbx.PBX_DB_PATH)
    caps = {k: lcr.decide(k, "353851234567")["cap"] for k in pbx.NUMBER_TYPES}
    out = {"settings.json": json.dumps({"settings": {k: v for k, v in pbx.get_settings(c).items() if k not in BOOKKEEPING},
                                        "lcr_caps": caps}, indent=1, sort_keys=True) + "\n",
           "pjsip_extensions.conf": pbx.build_extensions_conf(c),
           "extensions_portal.conf": pbx.build_portal_dialplan(c),
           "voicemail_portal.conf": pbx.build_voicemail_conf(c),
           "res_parking_portal.conf": pbx.build_parking_conf(c)}
    return {f: text.replace(data, "<DATA>") for f, text in out.items()}


if __name__ == "__main__":
    # Run as a script: seed into a scratch folder and print or save.
    import tempfile
    sys.path.insert(0, HERE)
    from conftest import env_for
    with tempfile.TemporaryDirectory() as d:
        os.environ.update(env_for(d))
        sys.path.insert(0, os.path.join(HERE, "..", "backend"))
        seed()
        got = build_all()
    if "--update" in sys.argv:
        os.makedirs(GOLDEN, exist_ok=True)
        for f in FILES:
            with open(os.path.join(GOLDEN, f), "w") as fh:
                fh.write(got[f])
        print("rewrote", GOLDEN)
    else:
        print({f: len(t) for f, t in got.items()})
