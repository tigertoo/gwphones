"""Every test runs against its own scratch folder and a fake Asterisk: no
real data, no .env, nothing reloaded. The modules read their settings when
they're imported, so each test imports them afresh."""
import logging
import os
import sys

import pytest

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
PORTAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "portal")
MODULES = ("main", "pbx", "lcr", "billing", "statements", "recordings", "ivr", "hours", "calllinks", "deskphones")
PORTAL_ORIGIN = "https://testserver"


def env_for(d: str) -> dict:
    for sub in ("gen", "rec", "ast", "vm-outbox"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return {
        "PBX_DB": os.path.join(d, "pbx.db"),
        "PBX_BILLING_DB": os.path.join(d, "billing.db"),
        "PBX_GENERATED_DIR": os.path.join(d, "gen"),
        "PBX_RECORDINGS_DIR": os.path.join(d, "rec"),
        "PBX_ASTERISK_DIR": os.path.join(d, "ast"),
        "PBX_VM_OUTBOX": os.path.join(d, "vm-outbox"),
        "PBX_CHANGES_LOG": os.path.join(d, "changes.log"),
        "PBX_CDR_CSV": os.path.join(d, "none.csv"),
        "PBX_ASTERISK_BIN": "/bin/false",  # every reload "fails": files are still written
        "PBX_PROVISION_BASE_URL": "https://pbx.example",
        "PBX_PUBLIC_HOST": "pbx.example",
        "PBX_TENANT_HOST_SUFFIX": ".sip.example.ie",
        "PBX_DISABLE_BACKGROUND": "1",
        "GWPHONES_TOKEN": "testtoken",
    }


def _reset_change_log():
    """pbx.py's change log is a logger, which outlives the module: without
    this every test would log into the first test's folder."""
    log = logging.getLogger("pbx_changes")
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in env_for(str(tmp_path)).items():
        monkeypatch.setenv(k, v)
    for k in ("PBX_ALLOW_NEW_DB", "PBX_LCR_URL", "GWPHONES_PROXY_SECRET", "GWPHONES_ALLOW_NO_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.syspath_prepend(BACKEND)
    _reset_change_log()
    for m in MODULES:
        sys.modules.pop(m, None)
    yield tmp_path
    _reset_change_log()
    for m in MODULES:
        sys.modules.pop(m, None)


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    c.headers.update({"Authorization": "Bearer testtoken", "X-PBX-Tenant": "1"})
    return c


@pytest.fixture
def portal_env(tmp_path, monkeypatch):
    """The portal's settings pointed at a scratch database; its modules
    imported afresh."""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("PORTAL_DB", str(tmp_path / "portal.db"))
    monkeypatch.setenv("PORTAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PORTAL_ORIGIN", PORTAL_ORIGIN)
    monkeypatch.setenv("PORTAL_GW_SECRET", "portal-test-secret")
    for k in ("MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET", "EMAIL_FROM_ADDRESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.syspath_prepend(PORTAL)
    for m in [m for m in sys.modules if m == "gwportal" or m.startswith("gwportal.")]:
        sys.modules.pop(m, None)
    yield tmp_path
    for m in [m for m in sys.modules if m == "gwportal" or m.startswith("gwportal.")]:
        sys.modules.pop(m, None)


@pytest.fixture
def portal(portal_env):
    """The portal app and a browser-like client on https (so the __Host-
    cookie is sent), whose changes carry the portal's Origin."""
    from fastapi.testclient import TestClient
    from gwportal import app as portal_app
    c = TestClient(portal_app.app, base_url=PORTAL_ORIGIN)
    c.headers.update({"Origin": PORTAL_ORIGIN})
    return portal_app, c
