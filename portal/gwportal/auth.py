"""Sign-in: passwords, two-factor codes, sessions, invitation and reset
links, lockouts.

- Passwords: argon2id. An unknown email is checked against a dummy hash,
  so the time taken doesn't tell whether it exists.
- Two-factor codes (TOTP): the secret encrypted with config.KEY; a code's
  time step can't be used twice; a pending sign-in gets MAX_CODE_TRIES.
- Sessions: a random token in the cookie, only its hash stored. States:
  full, pending_mfa (password right, code not yet given), enrol (an admin
  who must set up two-factor codes first). Every state change issues a new
  token. Idle and absolute limits from config.
- Links (invitations, resets): random, hashed, single-use, short-lived.
- Lockouts: an account after MAX_FAILURES in a row, for LOCK_MINUTES (a
  completed reset clears it); an address after IP_MAX_FAILURES in
  IP_WINDOW_MINUTES, kept in the database.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Optional

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from . import clock, config
from .db import iso

MAX_FAILURES = 5
LOCK_MINUTES = 15
IP_MAX_FAILURES = 20
IP_WINDOW_MINUTES = 15
MAX_CODE_TRIES = 5
PENDING_MINUTES = 10
INVITE_HOURS = 72
RESET_MINUTES = 60
MIN_PASSWORD = 12
RECOVERY_CODES = 10

_hasher = PasswordHasher()  # argon2id, the library's recommended parameters
_DUMMY = _hasher.hash(secrets.token_urlsafe(16))


class Refused(Exception):
    """A sign-in step refused; the message is safe to show."""
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


SIGN_IN_FAILED = "Wrong email or password, or the account is locked for a while."


def normal_email(email: str) -> str:
    return (email or "").strip().lower()


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -- passwords -----------------------------------------------------------------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def check_password_rules(password: str, email: str = ""):
    if not MIN_PASSWORD <= len(password or "") <= 200:
        raise Refused(f"Passwords need at least {MIN_PASSWORD} characters.", 400)
    if email and password.strip().lower() == normal_email(email):
        raise Refused("Your password can't be your email address.", 400)


def _password_ok(stored: Optional[str], password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY, password or "") and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# -- failures and lockouts --------------------------------------------------------

def ip_blocked(conn, ip: str) -> bool:
    since = iso(clock.now() - timedelta(minutes=IP_WINDOW_MINUTES))
    conn.execute("DELETE FROM failures WHERE at < ?", (iso(clock.now() - timedelta(days=1)),))
    return conn.execute("SELECT COUNT(*) FROM failures WHERE ip = ? AND at >= ?", (ip, since)).fetchone()[0] \
        >= IP_MAX_FAILURES


def note_ip_failure(conn, ip: str):
    conn.execute("INSERT INTO failures (ip, at) VALUES (?, ?)", (ip, iso()))


def _locked(user) -> bool:
    return bool(user["locked_until"]) and user["locked_until"] > iso()


def _note_account_failure(conn, user):
    failed = user["failed"] + 1
    locked = iso(clock.now() + timedelta(minutes=LOCK_MINUTES)) if failed >= MAX_FAILURES else user["locked_until"]
    conn.execute("UPDATE users SET failed = ?, locked_until = ? WHERE id = ?",
                 (0 if failed >= MAX_FAILURES else failed, locked, user["id"]))


# -- sessions ----------------------------------------------------------------------

def new_session(conn, user, state: str, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    now = clock.now()
    ttl = timedelta(minutes=PENDING_MINUTES) if state != "full" else timedelta(hours=config.SESSION_MAX_HOURS)
    conn.execute("INSERT INTO sessions (token_hash, user_id, state, created_at, last_seen, expires_at, ip) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (_h(token), user["id"], state, iso(now), iso(now), iso(now + ttl), ip))
    return token


def session_for(conn, token: str):
    """(session, user) for a cookie's token, or None: expired, idle too
    long, or the account disabled or locked out of its sessions."""
    if not token:
        return None
    s = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (_h(token),)).fetchone()
    if not s:
        return None
    now = clock.now()
    idle = timedelta(minutes=config.SESSION_IDLE_MINUTES if s["state"] == "full" else PENDING_MINUTES)
    if s["expires_at"] <= iso(now) or s["last_seen"] <= iso(now - idle):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (s["token_hash"],))
        return None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (s["user_id"],)).fetchone()
    if not user or user["disabled"]:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (s["token_hash"],))
        return None
    if s["last_seen"] <= iso(now - timedelta(minutes=1)):
        conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (iso(now), s["token_hash"]))
    return s, user


def end_session(conn, token: str):
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_h(token or ""),))


def end_all_sessions(conn, user_id: int, keep_token: str = ""):
    conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _h(keep_token)))


# -- signing in --------------------------------------------------------------------

def sign_in(conn, email: str, password: str, ip: str):
    """(token, state) for a right email and password, else Refused with
    the same message whatever was wrong."""
    if ip_blocked(conn, ip):
        raise Refused("Too many attempts from here. Try again in a while.", 429)
    user = conn.execute("SELECT * FROM users WHERE email = ?", (normal_email(email),)).fetchone()
    ok = _password_ok(user["pw_hash"] if user else None, password)
    if not user or not ok or user["disabled"] or _locked(user):
        note_ip_failure(conn, ip)
        if user and not ok and not _locked(user):
            _note_account_failure(conn, user)
        raise Refused(SIGN_IN_FAILED)
    conn.execute("UPDATE users SET failed = 0, locked_until = NULL WHERE id = ?", (user["id"],))
    if user["totp_secret"]:
        state = "pending_mfa"
    elif user["role"] == "admin":
        state = "enrol"
    else:
        state = "full"
    return new_session(conn, user, state, ip), state, user


def _fernet() -> Fernet:
    if not config.KEY:
        raise Refused("Two-factor codes aren't set up on this server yet (PORTAL_KEY).", 503)
    return Fernet(config.KEY.encode())


def _secret(enc: str) -> str:
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken:
        raise Refused("Two-factor codes can't be checked (the server's key changed).", 503)


def _code_step(secret: str, code: str, after: int) -> Optional[int]:
    """The time step a code is right for (now, or one either side), if it's
    later than `after` (a step can't be used twice)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now = int(clock.now().timestamp()) // 30
    for step in (now - 1, now, now + 1):
        if step > after and hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


def give_code(conn, session, user, code: str, recovery: str, ip: str) -> str:
    """The second step: a right code (or an unused recovery code) swaps the
    pending session for a full one (a new token). Too many wrong codes end
    the pending session and count towards the account's lockout."""
    if ip_blocked(conn, ip):
        raise Refused("Too many attempts from here. Try again in a while.", 429)
    ok = False
    if recovery:
        ok = _use_recovery_code(conn, user, recovery)
    else:
        step = _code_step(_secret(user["totp_secret"]), code, user["totp_last_step"])
        if step is not None:
            conn.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
            ok = True
    if not ok:
        note_ip_failure(conn, ip)
        _note_account_failure(conn, user)
        tries = session["code_attempts"] + 1
        if tries >= MAX_CODE_TRIES:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session["token_hash"],))
            raise Refused("Too many wrong codes. Sign in again.")
        conn.execute("UPDATE sessions SET code_attempts = ? WHERE token_hash = ?", (tries, session["token_hash"]))
        raise Refused("That code isn't right.")
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session["token_hash"],))
    conn.execute("UPDATE users SET failed = 0, locked_until = NULL WHERE id = ?", (user["id"],))
    return new_session(conn, user, "full", ip)


# -- setting up two-factor codes -------------------------------------------------------

def start_totp(conn, user) -> str:
    """A new secret, kept pending until a code from it is given; returns
    the otpauth:// address for the app (QR code)."""
    secret = pyotp.random_base32()
    conn.execute("UPDATE users SET totp_pending = ? WHERE id = ?",
                 (_fernet().encrypt(secret.encode()).decode(), user["id"]))
    return pyotp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name=config.BRAND)


def finish_totp(conn, user, code: str) -> list:
    """Turns the pending secret on if the code is right; returns new
    recovery codes (shown once)."""
    if not user["totp_pending"]:
        raise Refused("Start setting up two-factor codes first.", 400)
    secret = _secret(user["totp_pending"])
    step = _code_step(secret, code, 0)
    if step is None:
        raise Refused("That code isn't right. Check the time on your phone, and try the newest code.", 400)
    conn.execute("UPDATE users SET totp_secret = totp_pending, totp_pending = NULL, totp_last_step = ?, "
                 "updated_at = ? WHERE id = ?", (step, iso(), user["id"]))
    return new_recovery_codes(conn, user)


def turn_off_totp(conn, user):
    conn.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL, totp_last_step = 0, updated_at = ? "
                 "WHERE id = ?", (iso(), user["id"]))
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user["id"],))


def check_code(conn, user, code: str) -> bool:
    """A right, unused code for a signed-in user confirming something."""
    if not user["totp_secret"]:
        return False
    step = _code_step(_secret(user["totp_secret"]), code, user["totp_last_step"])
    if step is None:
        return False
    conn.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
    return True


def check_password(user, password: str) -> bool:
    return _password_ok(user["pw_hash"], password)


def new_recovery_codes(conn, user) -> list:
    codes = ["-".join(secrets.token_hex(2) for _ in range(3)) for _ in range(RECOVERY_CODES)]
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user["id"],))
    conn.executemany("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
                     [(user["id"], _h(c)) for c in codes])
    return codes


def _use_recovery_code(conn, user, code: str) -> bool:
    code = (code or "").strip().lower()
    return conn.execute("UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                        (iso(), user["id"], _h(code))).rowcount == 1


# -- invitation and reset links ------------------------------------------------------------

def new_link(conn, user, kind: str) -> str:
    token = secrets.token_urlsafe(32)
    ttl = timedelta(hours=INVITE_HOURS) if kind == "invite" else timedelta(minutes=RESET_MINUTES)
    conn.execute("DELETE FROM links WHERE user_id = ? AND kind = ? AND used_at IS NULL", (user["id"], kind))
    conn.execute("INSERT INTO links (token_hash, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
                 (_h(token), user["id"], kind, iso(clock.now() + ttl)))
    return token


def link_user(conn, token: str, kind: str):
    """The account a live link of this kind is for, or None."""
    r = conn.execute("SELECT * FROM links WHERE token_hash = ? AND kind = ? AND used_at IS NULL AND expires_at > ?",
                     (_h(token or ""), kind, iso())).fetchone()
    if not r:
        return None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (r["user_id"],)).fetchone()
    return None if not user or user["disabled"] else user


def use_link(conn, token: str, kind: str, password: str, ip: str):
    """Sets the password from an invitation or reset link (used once). A
    reset clears a lockout and ends every session; it never skips the
    two-factor code at the next sign-in."""
    user = link_user(conn, token, kind)
    if not user:
        note_ip_failure(conn, ip)
        raise Refused("This link has expired or was already used. Ask for a new one.", 400)
    check_password_rules(password, user["email"])
    conn.execute("UPDATE links SET used_at = ? WHERE token_hash = ?", (iso(), _h(token)))
    conn.execute("UPDATE users SET pw_hash = ?, failed = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
                 (hash_password(password), iso(), user["id"]))
    end_all_sessions(conn, user["id"])
    return user
