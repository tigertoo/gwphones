"""Every test runs against its own scratch folder and a fake Asterisk: no
real data, no .env, nothing reloaded. The modules read their settings when
they're imported, so each test imports them afresh."""
import logging
import os
import sys

import pytest

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
MODULES = ("main", "pbx", "lcr", "billing", "statements", "recordings", "ivr", "hours", "calllinks", "deskphones")


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
