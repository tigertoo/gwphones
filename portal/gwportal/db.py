"""The portal's own database: accounts, sessions, links, recovery codes,
failed sign-ins and the audit log. No phone data: that's gwphones'."""
import os
import sqlite3

from . import clock, config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,          -- stored lowercased: one login on the whole platform
    name TEXT NOT NULL DEFAULT '',
    tenant_code TEXT NOT NULL,           -- gwphones' tenant code, e.g. 1002
    role TEXT NOT NULL,                  -- admin | user
    ext TEXT,                            -- a user's extension, with the time it was created:
    ext_created TEXT,                    -- a later extension with the same number isn't theirs
    pw_hash TEXT,                        -- none until the invitation is accepted
    totp_secret TEXT,                    -- encrypted (config.KEY)
    totp_pending TEXT,                   -- being set up, encrypted
    totp_last_step INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    state TEXT NOT NULL,                 -- full | pending_mfa | enrol
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    code_attempts INTEGER NOT NULL DEFAULT 0,
    ip TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS links (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                  -- invite | reset
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    user_id INTEGER NOT NULL,
    code_hash TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS failures (    -- failed sign-ins and resets, per address
    ip TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER,
    email TEXT NOT NULL DEFAULT '',
    tenant_code TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trusted_devices (  -- "don't ask for a code here for 30 days"
    token_hash TEXT PRIMARY KEY,         -- the device cookie's token, hashed
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,            -- fixed: 30 days from when it was trusted
    last_used TEXT
);
CREATE INDEX IF NOT EXISTS trusted_devices_user ON trusted_devices (user_id);
CREATE TABLE IF NOT EXISTS passkeys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    credential_id TEXT NOT NULL UNIQUE,  -- base64url, as the browser sends it
    public_key BLOB NOT NULL,            -- COSE
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used TEXT
);
CREATE TABLE IF NOT EXISTS webauthn_challenges (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                  -- register | login
    user_id INTEGER,                     -- register: whose
    challenge BLOB NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ms_flows (       -- a sign-in with Microsoft under way (microsoft.py)
    token_hash TEXT PRIMARY KEY,         -- its Lax cookie's token, hashed
    purpose TEXT NOT NULL,               -- signin | link
    user_id INTEGER,                     -- link: the admin who started it
    state TEXT NOT NULL,
    nonce TEXT NOT NULL,
    verifier TEXT NOT NULL,              -- PKCE
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ms_links (       -- a tenant's Microsoft 365, and what it syncs
    tenant_code TEXT PRIMARY KEY,
    ms_tenant TEXT NOT NULL UNIQUE,      -- the Microsoft organisation (tid): one portal tenant each
    group_id TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    admin_group_id TEXT NOT NULL DEFAULT '',
    admin_group_name TEXT NOT NULL DEFAULT '',
    attribute INTEGER NOT NULL DEFAULT 1, -- Exchange custom attribute N: the extension
    linked_by TEXT NOT NULL DEFAULT '',
    linked_at TEXT NOT NULL DEFAULT '',
    last_sync_at TEXT,
    last_result TEXT
);
CREATE INDEX IF NOT EXISTS passkeys_user ON passkeys (user_id);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS failures_ip ON failures (ip, at);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    fresh = not os.path.exists(config.DB_PATH)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    if fresh:
        os.chmod(config.DB_PATH, 0o600)  # password hashes and encrypted two-factor secrets
    conn.executescript(SCHEMA)
    for migration in ("ALTER TABLE users ADD COLUMN webauthn_handle TEXT",  # passkeys' user handle
                      # Microsoft 365 (microsoft.py): the account's organisation and object id,
                      # and 'microsoft' for people its sync manages.
                      "ALTER TABLE users ADD COLUMN ms_tenant TEXT",
                      "ALTER TABLE users ADD COLUMN ms_oid TEXT",
                      "ALTER TABLE users ADD COLUMN source TEXT NOT NULL DEFAULT 'local'",
                      # who disabled someone: 'admin' (Staff, the CLI) or 'sync' (Microsoft 365):
                      # sync only ever enables again the people it disabled itself
                      "ALTER TABLE users ADD COLUMN disabled_by TEXT",
                      # Microsoft 365 sync may create missing extensions (per tenant, off by default)
                      "ALTER TABLE ms_links ADD COLUMN create_exts INTEGER NOT NULL DEFAULT 0",
                      # extensions sync switched off for a leaver: only these does it switch on again
                      "CREATE TABLE IF NOT EXISTS ms_switched_off (tenant_code TEXT NOT NULL, ext TEXT NOT NULL, "
                      "PRIMARY KEY (tenant_code, ext))",
                      "CREATE UNIQUE INDEX IF NOT EXISTS users_ms ON users (ms_tenant, ms_oid)"):
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # already there
    return conn


def iso(dt=None) -> str:
    return (dt or clock.now()).isoformat()


def audit(conn, action: str, user=None, ip: str = "", detail: str = ""):
    conn.execute("INSERT INTO audit (at, user_id, email, tenant_code, ip, action, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (iso(), user["id"] if user else None, user["email"] if user else "",
                  user["tenant_code"] if user else "", ip, action, detail[:500]))
