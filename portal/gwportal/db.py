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
    return conn


def iso(dt=None) -> str:
    return (dt or clock.now()).isoformat()


def audit(conn, action: str, user=None, ip: str = "", detail: str = ""):
    conn.execute("INSERT INTO audit (at, user_id, email, tenant_code, ip, action, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (iso(), user["id"] if user else None, user["email"] if user else "",
                  user["tenant_code"] if user else "", ip, action, detail[:500]))
