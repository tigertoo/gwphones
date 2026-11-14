"""The time, in one place, so tests can move it (lockouts, links, sessions,
two-factor codes) without waiting."""
from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)
