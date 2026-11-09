"""The empty-database guard, PBX_LCR_URL and PBX_DISABLE_BACKGROUND."""
import asyncio
import os

import pytest


def test_empty_database_refused_once_a_config_exists(env, monkeypatch):
    import pbx
    pbx.apply_config(pbx.get_db(), "test")
    assert os.path.exists(pbx.EXT_CONF)
    before = open(pbx.EXT_CONF).read()
    os.remove(pbx.PBX_DB_PATH)
    with pytest.raises(pbx.MissingDatabase):
        pbx.get_db()
    assert not os.path.exists(pbx.PBX_DB_PATH)
    asyncio.run(pbx.apply_on_startup(attempts=1, wait=0))
    assert open(pbx.EXT_CONF).read() == before and not os.path.exists(pbx.PBX_DB_PATH)
    assert "isn't at" in open(pbx.CHANGES_LOG_PATH).read()
    monkeypatch.setenv("PBX_ALLOW_NEW_DB", "1")
    assert pbx.get_db() is not None


def test_route_check_address(env, monkeypatch):
    monkeypatch.setenv("PBX_LCR_URL", "http://127.0.0.1:9999/lcr/route")
    import pbx
    pbx.apply_config(pbx.get_db(), "test")
    assert "CURL(http://127.0.0.1:9999/lcr/route?" in open(pbx.PORTAL_DIALPLAN).read()


@pytest.mark.parametrize("disabled", [True, False])
def test_background_switch(env, monkeypatch, disabled):
    if not disabled:
        monkeypatch.delenv("PBX_DISABLE_BACKGROUND")
    import pbx, recordings, billing
    calls = []

    async def fake(*a, **k):
        calls.append(1)
    for mod, name in ((pbx, "apply_on_startup"), (pbx, "voicemail_mail_loop"),
                      (recordings, "recordings_expiry_loop"), (billing, "billing_loop")):
        monkeypatch.setattr(mod, name, fake)
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app):
        pass
    assert len(calls) == (0 if disabled else 4)
