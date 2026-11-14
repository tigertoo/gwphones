"""Email through Microsoft Graph, with the portal's own copy of the
settings (the same app registration gwphones uses). Links in emails are
built only from config.ORIGIN, never from a request's Host."""
import html
import os

import httpx

from . import config

TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "")
CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MS_GRAPH_CLIENT_SECRET", "")
FROM = os.environ.get("EMAIL_FROM_ADDRESS", "")


class NotSent(Exception):
    pass


def ready() -> bool:
    return bool(TENANT_ID and CLIENT_ID and CLIENT_SECRET and FROM and config.ORIGIN)


def send(to: str, subject: str, body_html: str):
    if not ready():
        raise NotSent("Email isn't set up on this server.")
    try:
        with httpx.Client(timeout=20) as client:
            tok = client.post(f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token", data={
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
            tok.raise_for_status()
            res = client.post(f"https://graph.microsoft.com/v1.0/users/{FROM}/sendMail",
                              headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
                              json={"message": {"subject": subject, "body": {"contentType": "HTML", "content": body_html},
                                                "toRecipients": [{"emailAddress": {"address": to}}]},
                                    "saveToSentItems": True})
            res.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Graph's status only, never the request (it holds the secret).
        raise NotSent(f"Microsoft Graph said {e.response.status_code}")
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise NotSent(type(e).__name__)


def link(page: str, token: str) -> str:
    """A link to one of the portal's pages carrying a token in the
    fragment (#...), which browsers never send to the server: it stays out
    of logs and Referer headers."""
    return f"{config.ORIGIN}/{page}#{token}"


def _page(heading: str, lines: list, button: str, url: str) -> str:
    esc = html.escape
    btn = ("display:inline-block;padding:12px 20px;border-radius:8px;background:#0a7cff;color:#ffffff;"
           "text-decoration:none;font-weight:600")
    return (f'<div style="font:15px -apple-system,Segoe UI,sans-serif;color:#222;max-width:560px">'
            f'<h2 style="font-size:18px">{esc(heading)}</h2>'
            + "".join(f"<p>{esc(l)}</p>" for l in lines)
            + f'<p><a href="{esc(url)}" style="{btn}">{esc(button)}</a></p>'
            f'<p style="color:#666">Or paste this into your browser: {esc(url)}</p></div>')


def invite(user, tenant_name: str, token: str, inviter: str = ""):
    by = f"{inviter} has invited you" if inviter else "You've been invited"
    send(user["email"], f"Your {config.BRAND} phone system account",
         _page(f"{config.BRAND}: {tenant_name}",
               [f"{by} to manage {tenant_name}'s phone system.",
                "Choose a password to start. The link works once, for 72 hours."],
               "Choose a password", link("invite", token)))


def reset(user, token: str):
    send(user["email"], f"Reset your {config.BRAND} password",
         _page(f"{config.BRAND}: reset your password",
               ["Someone (hopefully you) asked to reset your password.",
                "The link works once, for 60 minutes. If it wasn't you, ignore this email: nothing changes."],
               "Choose a new password", link("reset", token)))
