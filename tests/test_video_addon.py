"""Video is a paid add-on, each tenant's own (TENANT_SETTINGS "video"):
its extensions' video codecs and its call links' camera. Off for a new
tenant; set in the Tenants tab."""
import json
import re

from fastapi.testclient import TestClient

from test_portal_caller import two  # noqa: F401  (main tenant and Acme, both with a 201)


def endpoint(pbx, login):
    return open(pbx.EXT_CONF).read().split(f"[{login}](endpoint-ext)")[1].split("\n[")[0]


def add_link(pbx, tid, label, video=True):
    import calllinks
    token = pbx._current_tenant.set(tid)
    try:
        calllinks.api_add_call_link(calllinks.CallLinkRequest(label=label, target="201", video=video))
    finally:
        pbx._current_tenant.reset(token)
    return pbx.get_db().execute("SELECT token FROM call_links WHERE label = ?", (label,)).fetchone()["token"]


def page_video(main, token):
    html = TestClient(main.app).get(f"/call/{token}").text
    cfg = json.loads(re.search(r'<script type="application/json" id="config">(.*?)</script>', html, re.S).group(1)
                     .replace("<\\/", "</"))
    return cfg["video"], 'id="video"' in html


def test_each_tenants_own(two):
    main, pbx, op, portal, acme = two
    code = pbx.tenant_names(pbx.get_db(), acme).code
    s = op.get("/api/pbx/config").json()["settings"]
    assert op.put("/api/pbx/settings", json={**s, "video": True}).status_code == 200  # the main tenant's, as before
    assert "allow=h264,vp8" in endpoint(pbx, "201")
    assert "allow=h264" not in endpoint(pbx, f"{code}201")  # Acme hasn't got the add-on
    assert portal.get("/api/pbx/tenant").json()["video"] is False
    assert portal.get("/api/pbx/call-links").json()["video_on"] is False
    listed = {t["code"]: t["settings"]["video"] for t in op.get("/api/pbx/tenants").json()["tenants"]}
    assert listed == {"1001": "1", code: "0"}

    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "video": True}).status_code == 200
    assert "allow=h264,vp8" in endpoint(pbx, f"{code}201")
    assert portal.get("/api/pbx/tenant").json()["video"] is True
    # leaving it out of an edit keeps it; the main tenant's untouched by Acme's
    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme Ltd"}).status_code == 200
    assert "allow=h264,vp8" in endpoint(pbx, f"{code}201")
    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "video": False}).status_code == 200
    assert "allow=h264" not in endpoint(pbx, f"{code}201") and "allow=h264,vp8" in endpoint(pbx, "201")


def test_new_tenant_with_it(two):
    main, pbx, op, portal, acme = two
    assert op.post("/api/pbx/tenants", json={"name": "Vid", "outbound_cid": "015557777", "video": True}).status_code == 200
    vid = pbx.get_db().execute("SELECT id FROM tenants WHERE name = 'Vid'").fetchone()["id"]
    assert pbx.get_settings(pbx.get_db(), vid)["video"] == "1"
    assert pbx.get_settings(pbx.get_db(), acme)["video"] == "0"  # the default


def test_call_links_need_it(two):
    main, pbx, op, portal, acme = two
    token = add_link(pbx, acme, "Acme site", video=True)
    assert page_video(main, token) == (False, False)  # no add-on: no camera, whatever the link says
    assert op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "video": True}).status_code == 200
    assert page_video(main, token) == (True, True)
    audio = add_link(pbx, acme, "Acme audio", video=False)
    assert page_video(main, audio) == (False, False)  # the link's own setting still counts


def test_browsers_get_video_codecs_only_for_it(two):
    main, pbx, op, portal, acme = two
    add_link(pbx, acme, "Acme site", video=True)
    guest = "\n".join(pbx._web_guest_endpoint(pbx.get_db()))
    assert "allow=alaw,ulaw\n" in guest + "\n" and "h264" not in guest
    op.put(f"/api/pbx/tenants/{acme}", json={"name": "Acme", "video": True})
    assert "h264" in "\n".join(pbx._web_guest_endpoint(pbx.get_db()))


def test_server_setting_moves_to_the_main_tenant(env):
    """Until now video was the server's (and so every tenant's): it becomes
    the main tenant's alone, at the next start."""
    import pbx
    conn = pbx.get_db()
    conn.execute("DELETE FROM tenant_settings WHERE key = 'video'")
    conn.execute("INSERT INTO settings (key, value) VALUES ('video', '1')")
    conn.commit()
    conn = pbx.get_db()  # opening runs the migrations
    assert conn.execute("SELECT 1 FROM settings WHERE key = 'video'").fetchone() is None
    assert pbx.get_settings(conn, pbx.MAIN_TENANT)["video"] == "1"
    assert pbx.get_settings(conn, 999)["video"] == "0"
