"""Settings, from the environment (the service's .env)."""
import os

DB_PATH = os.environ.get("PORTAL_DB", "/var/lib/gwportal/portal.db")
# The portal's own address, e.g. https://portal.example.ie: the only base
# for links in emails (never the request's Host), and the only Origin
# changes are accepted from.
ORIGIN = os.environ.get("PORTAL_ORIGIN", "").rstrip("/")
# Encrypts the two-factor secrets in the database (a Fernet key: make one
# with python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").
KEY = os.environ.get("PORTAL_KEY", "")
# gwphones' API and the portal's credential for it (its GWPHONES_PORTAL_SECRET).
GW_URL = os.environ.get("PORTAL_GW_URL", "http://127.0.0.1:8010").rstrip("/")
GW_SECRET = os.environ.get("PORTAL_GW_SECRET", "")
BRAND = os.environ.get("PORTAL_BRAND", "gwphones")

SESSION_IDLE_MINUTES = int(os.environ.get("PORTAL_SESSION_IDLE_MINUTES", "120"))
SESSION_MAX_HOURS = int(os.environ.get("PORTAL_SESSION_MAX_HOURS", "12"))
COOKIE = "__Host-gwportal"
