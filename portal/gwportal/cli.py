"""The operator's command for the portal, run on the server (nothing of it
is reachable from the internet):

    venv/bin/python -m gwportal.cli invite --tenant 1002 --email anne@acme.ie --name "Anne Byrne"
    venv/bin/python -m gwportal.cli list --tenant 1002
    venv/bin/python -m gwportal.cli reset-2fa --email anne@acme.ie
    venv/bin/python -m gwportal.cli disable --email anne@acme.ie     (and enable)

invite makes a tenant's first admin (who then invites their own staff),
after checking with gwphones that the tenant exists and is active, and
emails the invitation; --print-link shows the link here instead, to pass
on yourself. reset-2fa is for an admin who lost their phone and their
recovery codes: their two-factor setup is cleared and they're signed out,
so they set it up again at their next sign-in.

It reads the portal's .env (--env, default /opt/gwphones/portal/.env, or
PORTAL_ENV_FILE) before anything else, so it uses the service's settings.
Run it as the portal's own user.
"""
import argparse
import os
import sys

DEFAULT_ENV = "/opt/gwphones/portal/.env"


def load_env(path: str):
    """KEY=value lines into the environment (the format systemd reads)."""
    if not path or not os.path.exists(path):
        return
    for line in open(path):
        key, sep, value = line.rstrip("\n").partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value)


OPERATOR = "operator (gwportal command)"


def _operator(tenant_code: str) -> dict:
    """Who gwphones is told is acting, for its change log."""
    return {"tenant_code": tenant_code, "email": OPERATOR}


def cmd_invite(args) -> int:
    from . import auth, db, gw, mail
    from .app import create_user
    try:
        tenant = gw.call(_operator(args.tenant), "GET", "/tenant")
    except auth.Refused as e:
        print(f"Tenant {args.tenant}: {e}", file=sys.stderr)
        return 1
    if tenant["status"] != "active":
        print(f"Tenant {args.tenant} ({tenant['name']}) is suspended: reactivate it first.", file=sys.stderr)
        return 1
    if not args.print_link and not mail.ready():
        print("Email isn't set up for the portal (MS_GRAPH_*, EMAIL_FROM_ADDRESS, PORTAL_ORIGIN): "
              "use --print-link to pass the link on yourself.", file=sys.stderr)
        return 1
    with db.connect() as conn:
        try:
            user = create_user(conn, args.email, args.tenant, "admin", args.name or "")
        except auth.Refused as e:
            print(str(e), file=sys.stderr)
            return 1
        token = auth.new_link(conn, user, "invite")
        db.audit(conn, "first admin invited", None, "", f"{user['email']} for {args.tenant} by the operator")
    if args.print_link:
        print(f"Invited {user['email']} as {tenant['name']}'s admin. The link (works once, for 72 hours):")
        print(mail.link("invite", token))
        return 0
    try:
        mail.invite(user, tenant["name"], token)
    except mail.NotSent as e:
        with db.connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        print(f"The invitation couldn't be emailed ({e}): nothing was kept. Try again, or use --print-link.",
              file=sys.stderr)
        return 1
    print(f"Invited {user['email']} as {tenant['name']}'s admin: the invitation is on its way.")
    return 0


def cmd_list(args) -> int:
    from . import db
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users WHERE tenant_code = ? ORDER BY role, email", (args.tenant,)).fetchall()
    if not rows:
        print(f"No portal accounts for tenant {args.tenant}.")
    for u in rows:
        state = "disabled" if u["disabled"] else ("invited" if not u["pw_hash"] else "active")
        print(f"{u['email']:40} {u['role']:6} ext {u['ext'] or '-':4} two-factor {'on ' if u['totp_secret'] else 'off'} {state}")
    return 0


def _one(conn, email: str):
    from . import auth
    u = conn.execute("SELECT * FROM users WHERE email = ?", (auth.normal_email(email),)).fetchone()
    if not u:
        print(f"No portal account {email}.", file=sys.stderr)
    return u


def cmd_reset_2fa(args) -> int:
    from . import auth, db
    with db.connect() as conn:
        u = _one(conn, args.email)
        if not u:
            return 1
        auth.turn_off_totp(conn, u)
        auth.end_all_sessions(conn, u["id"])
        db.audit(conn, "two-factor reset by the operator", u)
    print(f"{u['email']}: two-factor codes cleared and signed out everywhere."
          + (" They set them up again at their next sign-in." if u["role"] == "admin" else ""))
    return 0


def cmd_disable(args, disabled: bool) -> int:
    from . import auth, db
    with db.connect() as conn:
        u = _one(conn, args.email)
        if not u:
            return 1
        conn.execute("UPDATE users SET disabled = ?, updated_at = ? WHERE id = ?", (int(disabled), db.iso(), u["id"]))
        if disabled:
            auth.end_all_sessions(conn, u["id"])
        db.audit(conn, ("disabled" if disabled else "enabled") + " by the operator", u)
    print(f"{u['email']}: {'disabled and signed out' if disabled else 'enabled'}.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gwportal.cli", description="The operator's portal command.")
    p.add_argument("--env", default=os.environ.get("PORTAL_ENV_FILE", DEFAULT_ENV), help="the portal's .env")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("invite", help="invite a tenant's first admin")
    i.add_argument("--tenant", required=True, help="the tenant's code, e.g. 1002")
    i.add_argument("--email", required=True)
    i.add_argument("--name", default="")
    i.add_argument("--print-link", action="store_true", help="show the link here instead of emailing it")
    sub.add_parser("list", help="a tenant's portal accounts").add_argument("--tenant", required=True)
    sub.add_parser("reset-2fa", help="clear someone's two-factor setup").add_argument("--email", required=True)
    sub.add_parser("disable", help="disable an account and sign it out").add_argument("--email", required=True)
    sub.add_parser("enable", help="enable an account again").add_argument("--email", required=True)
    args = p.parse_args(argv)
    load_env(args.env)  # before the modules read their settings
    if getattr(args, "tenant", None) is not None and not (len(args.tenant) == 4 and args.tenant.isdigit()):
        print("A tenant code is four digits, e.g. 1002.", file=sys.stderr)
        return 2
    return {"invite": cmd_invite, "list": cmd_list, "reset-2fa": cmd_reset_2fa,
            "disable": lambda a: cmd_disable(a, True), "enable": lambda a: cmd_disable(a, False)}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
