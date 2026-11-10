"""main.py: who may call the API, and the tenant switcher."""
from fastapi.testclient import TestClient


def test_refused_without_credentials(env):
    import main
    c = TestClient(main.app)
    assert c.get("/api/pbx/config").status_code == 401
    assert c.get("/api/pbx/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/health").status_code == 200


def test_proxy_secret(env, monkeypatch):
    monkeypatch.setenv("GWPHONES_PROXY_SECRET", "s3cret")
    monkeypatch.delenv("GWPHONES_TOKEN")
    import main
    c = TestClient(main.app)
    assert c.get("/api/pbx/config", headers={"X-Proxy-Secret": "s3cret"}).status_code == 200
    assert c.get("/api/pbx/config", headers={"X-Proxy-Secret": "nope"}).status_code == 401
    # the old dashboard's header name means nothing here
    assert c.get("/api/pbx/config", headers={"X-Admin-Proxy-Secret": "s3cret"}).status_code == 401


def test_nothing_open_by_default(env, monkeypatch):
    monkeypatch.delenv("GWPHONES_TOKEN")
    import main
    assert TestClient(main.app).get("/api/pbx/config").status_code == 401


def test_changes_from_another_site_refused(client):
    r = client.post("/api/pbx/apply", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_tenant_switcher(client):
    assert client.post("/api/pbx/apply", headers={"X-PBX-Tenant": ""}).status_code == 409
    client.cookies.set("pbx_tenant", "77")
    assert client.get("/api/pbx/config").status_code == 400


def test_public_pages_outside_the_check(env):
    import main
    c = TestClient(main.app)
    assert c.get("/prov/nonexistent/account.xml").status_code != 401
    assert c.get("/call/nonexistent").status_code != 401


def test_call_link_page_assets(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not started from backend/
    import main
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    for name, kind in (("call.js", "javascript"), ("call.css", "css"), ("jssip-3.13.8.min.js", "javascript")):
        r = c.get(f"/call/static/{name}")
        assert r.status_code == 200 and kind in r.headers["content-type"] and len(r.content) > 100, name


def test_no_api_description(env):
    import main
    c = TestClient(main.app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert c.get(path).status_code == 404, path
