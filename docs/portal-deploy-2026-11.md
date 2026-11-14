# Putting the tenant portal on the server (phase 4, step 4)

The runbook for the development VPS: the portal (`gwportal`) as its own
user and service, its secrets, a DNS name and certificate, the nginx
site, then a first admin for Test (1002). About 45 minutes; nothing here
touches calls. Run everything as root.

What it ends with: `https://portal.amapforyourmoney.com`, served by
nginx, the API on 127.0.0.1:8020 as `gwportal`, which reaches gwphones
(127.0.0.1:8010) only with the portal's secret.

**Before you start:** gwphones and the admin dashboard both pushed (this
runbook, the service and nginx examples, and the backup script).

`PORTAL_BRAND=gwphones` (P3) is the name the invitation emails and
people's authenticator apps will show until the brand is chosen.

## P0. Checks first (read only)

```bash
nginx -v
cat /etc/letsencrypt/renewal-hooks/deploy/asterisk-cert.sh | grep -nE 'RENEWED|pbx\.|exit'
curl -4 -s ifconfig.me; echo; curl -6 -s ifconfig.me; echo
grep -c '^GWPHONES_PORTAL_SECRET=' /opt/gwphones/backend/.env
id gwportal 2>&1 | head -1
```

- **The Asterisk certificate hook must only act on the PBX's own
  certificate.** certbot runs every script in `renewal-hooks/deploy/`
  for every certificate it renews. A hook that doesn't leave when the
  certificate isn't `pbx.amapforyourmoney.com` would, at the portal
  certificate's first renewal (about 60 days on), copy the portal's
  certificate into Asterisk and break the phones' TLS, or at least
  restart Asterisk. **Unless the grep shows the hook already exits for
  other certificates, add the filter (P0a).**
- The two addresses are for the DNS records (P5).
- `0` for the secret, and no `gwportal` user yet.

### P0a. The filter (unless the hook already has one)

Insert this right after its first line (`#!/bin/sh`), so it ignores
every other certificate:

```bash
sed -i '1a case "$RENEWED_LINEAGE" in */pbx.amapforyourmoney.com) ;; *) exit 0 ;; esac' /etc/letsencrypt/renewal-hooks/deploy/asterisk-cert.sh
head -3 /etc/letsencrypt/renewal-hooks/deploy/asterisk-cert.sh
```

## P1. The code, built and tested

```bash
git -C /opt/gwphones pull
cd /opt/gwphones/portal && python3 -m venv venv && venv/bin/pip install -q -r requirements.txt
cd /opt/gwphones/portal/frontend && npm ci --no-audit --no-fund 2>&1 | tail -1 && npm run build 2>&1 | grep -E 'built|rror'
python3 -m venv /root/gwtest && /root/gwtest/bin/pip install -q -r /opt/gwphones/backend/requirements.txt \
  -r /opt/gwphones/portal/requirements.txt -r /opt/gwphones/backend/requirements-dev.txt
cd /opt/gwphones && /root/gwtest/bin/pytest 2>&1 | tail -1
```

Expect `built` and `120 passed`. The tests run in a throwaway
environment (`/root/gwtest`), never gwphones' own.

This pull also brings gwphones' tenant-code change for the portal (the
portal names its tenant by code), which takes effect at P3's restart;
P3's check confirms it.

## P2. Its own user and folder

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin gwportal
install -d -o gwportal -g gwportal -m 700 /var/lib/gwportal
id gwportal
```

## P3. The secrets (nothing printed)

The portal's secret goes into both `.env` files; the portal gets a new
encryption key for two-factor secrets, and its own copy of the email
settings.

```bash
umask 077
SECRET=$(openssl rand -hex 32)
echo "GWPHONES_PORTAL_SECRET=$SECRET" >> /opt/gwphones/backend/.env
KEY=$(/opt/gwphones/portal/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
cat > /opt/gwphones/portal/.env <<EOF
PORTAL_DB=/var/lib/gwportal/portal.db
PORTAL_ORIGIN=https://portal.amapforyourmoney.com
PORTAL_KEY=$KEY
PORTAL_GW_URL=http://127.0.0.1:8010
PORTAL_GW_SECRET=$SECRET
PORTAL_BRAND=gwphones
EOF
grep -E '^(MS_GRAPH_|EMAIL_FROM_ADDRESS=)' /opt/gwphones/backend/.env >> /opt/gwphones/portal/.env
unset SECRET KEY
chown root:gwportal /opt/gwphones/portal/.env && chmod 640 /opt/gwphones/portal/.env
umask 022   # back to normal: later files must be readable (nginx, the build)
stat -c '%U:%G %a' /opt/gwphones/portal/.env
sed -n 's/^\([A-Z_]*\)=.*/\1/p' /opt/gwphones/portal/.env
[ "$(sed -n 's/^GWPHONES_PORTAL_SECRET=//p' /opt/gwphones/backend/.env | md5sum)" = "$(sed -n 's/^PORTAL_GW_SECRET=//p' /opt/gwphones/portal/.env | md5sum)" ] && echo "secrets match"
```

Expect `root:gwportal 640`, the ten names, and `secrets match`. The
portal's `.env` is readable by its own user, so the operator's command
can run as `gwportal` (P8).

**Keep `PORTAL_KEY` safe:** it's backed up with the portal (P9).
Without it, everyone's two-factor codes would have to be set up again.

Then restart gwphones, so it knows the portal's secret, and check the
portal can reach Test:

```bash
systemctl restart gwphones && sleep 15 && systemctl is-active gwphones
curl -s -H "X-Portal-Secret: $(sed -n 's/^GWPHONES_PORTAL_SECRET=//p' /opt/gwphones/backend/.env)" -H 'X-Portal-Tenant: 1002' http://127.0.0.1:8010/api/pbx/tenant; echo
```

Expect `active`, then `{"code":"1002","name":"Test","status":"active"}`:
that also confirms gwphones' tenant-code change is live. In the console,
Re-apply: the files are unchanged.

## P4. The service

```bash
cp /opt/gwphones/portal/gwportal.service.example /etc/systemd/system/gwportal.service
systemctl daemon-reload && systemctl enable --now gwportal && sleep 5
systemctl is-active gwportal
ss -ltnp | grep -E ':80(10|11|20)\b'
curl -s http://127.0.0.1:8020/api/session; echo
sudo -u gwportal cat /var/lib/admin-dashboard/pbx.db > /dev/null 2>&1 && echo "CAN READ PHONE DATA: STOP" || echo "phone data out of reach"
stat -c '%U %a' /var/lib/gwportal/portal.db
```

Expect `active`; three listeners (gwphones 8010, dashboard 8011, portal
8020); `{"state":"none"}`; `phone data out of reach`; `gwportal 600`.

## P5. The DNS name (Cloudflare)

In Cloudflare, add for `amapforyourmoney.com`:

- `A` record, name `portal`, the IPv4 address from P0, **DNS only**
  (grey cloud)
- `AAAA` record, name `portal`, the IPv6 address from P0, DNS only

DNS only, not proxied: through Cloudflare's proxy the portal would see
Cloudflare's addresses instead of people's, and its per-address limits
and audit log would be wrong. Then:

```bash
sleep 60; dig +short portal.amapforyourmoney.com A; dig +short portal.amapforyourmoney.com AAAA
```

## P6. The certificate

Its own certificate (not added to the PBX's, whose renewal restarts
Asterisk), by the same Cloudflare DNS check as the PBX's, with its own
hook to reload nginx when it renews (saved for this certificate only):

```bash
md5sum /etc/asterisk/keys/* > /root/asterisk-keys-before.md5
asterisk -rx "core show uptime" | head -1
certbot certonly --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d portal.amapforyourmoney.com --cert-name portal.amapforyourmoney.com --deploy-hook "systemctl reload nginx"
certbot renew --cert-name portal.amapforyourmoney.com --dry-run 2>&1 | grep -iE 'success|fail|error'
```

Expect `Successfully received certificate`, then the dry run's
`Congratulations, all simulated renewals succeeded`.

Then prove the Asterisk hook leaves the portal's certificate alone, by
running it as certbot would at a renewal. **Only with P0a's filter in
place** (or the hook's own check confirmed):

```bash
RENEWED_LINEAGE=/etc/letsencrypt/live/portal.amapforyourmoney.com RENEWED_DOMAINS=portal.amapforyourmoney.com \
  sh /etc/letsencrypt/renewal-hooks/deploy/asterisk-cert.sh; echo "hook exit: $?"
md5sum -c /root/asterisk-keys-before.md5 && echo "Asterisk's certificate untouched"
asterisk -rx "core show uptime" | head -1
```

Expect `hook exit: 0`, every key `OK`, `Asterisk's certificate untouched`,
and the same uptime as before (no restart). If anything else, tell me
before going on.

## P7. The nginx site

```bash
cp /opt/gwphones/portal/nginx-site.example /etc/nginx/sites-available/portal.amapforyourmoney.com
ln -s /etc/nginx/sites-available/portal.amapforyourmoney.com /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
curl -s -o /dev/null -w 'page: %{http_code}\n' https://portal.amapforyourmoney.com/
curl -s https://portal.amapforyourmoney.com/api/session; echo
curl -sI https://portal.amapforyourmoney.com/ | grep -iE 'strict-transport|content-security|x-frame'
curl -s -o /dev/null -w 'http: %{http_code} -> %{redirect_url}\n' http://portal.amapforyourmoney.com/
for i in $(seq 1 12); do curl -s -o /dev/null -w '%{http_code} ' -X POST -H 'Content-Type: application/json' \
  -H 'Origin: https://portal.amapforyourmoney.com' -d '{"email":"nobody@example.com","password":"x"}' \
  https://portal.amapforyourmoney.com/api/login; done; echo
```

Expect `page: 200`; `{"state":"none"}`; the three security headers; a
301 to https; and in the last line some `401`s, then `429`s (nginx's
limit). A minute later the address is let through again.

Then open `https://portal.amapforyourmoney.com` in a browser: the sign-in
page should show. A blank page would mean the security policy blocks
something; the browser's console would say what.

## P8. A first admin for Test (1002)

Use a **secondary or "plus" address** you can read on your phone (e.g.
`you+test@…`): an email is one login on the whole platform, so keep your
main address free for your own business's portal account later.

```bash
cd /opt/gwphones/portal && sudo -u gwportal venv/bin/python -m gwportal.cli invite --tenant 1002 --email YOU+TEST@EXAMPLE --name "YOUR NAME"
```

Expect `the invitation is on its way`. On your phone, from the email:

1. Choose a password.
2. Sign in, and set up two-factor codes with your authenticator app.
3. Keep the recovery codes.

Then try it as Test's admin:

- **Extensions:** only Test's extensions, **never your own business's
  (1001)**: the first check across tenants on the live server. Add a
  second one (e.g. "Second desk", it gets 202) if Test has only 201.
  Rename one, set up a phone by QR code.
- **Status, Recordings and Account.**
- **Staff:** invite someone, with another plus address, as staff on
  Test's 201. Sign in as them on another device: they should see only
  201 (not 202), and no Staff or Delete.

Check that gwphones' change log names who did it:

```bash
grep -F '[portal:' /var/log/admin-dashboard/pbx-changes.log | tail -3
```

## P9. Backups

The backup script (in the admin dashboard repository) now also copies
the portal's database, its `.env`, its service file and nginx site:

```bash
git -C /opt/admin-dashboard pull && cp /opt/admin-dashboard/backup-vital-files.sh /root/backup-vital-files.sh
bash /root/backup-vital-files.sh 2>&1 | grep -E 'portal|WARNING|complete'
tar -tzf "$(ls -t /root/backups/finance-app-backup_*.tar.gz | head -1)" | grep -E 'gwportal|portal\.amap'
```

Expect no WARNING, and `gwportal/portal.db`, `gwportal/env`,
`gwportal.service` and the nginx site in the archive.

## Rollback

Nothing else depends on the portal, so taking it away is:

```bash
systemctl disable --now gwportal
rm /etc/nginx/sites-enabled/portal.amapforyourmoney.com && nginx -t && systemctl reload nginx
```

The secret in gwphones' `.env` is harmless without the portal (or
remove the line and restart gwphones). The DNS record and certificate
can stay.

## Later

- Moving to the brand's domain: the name in the nginx site,
  `PORTAL_ORIGIN`, a certificate and DNS records for it.
- The alert to the operator for a save that couldn't be applied is a
  log line for now (`journalctl -u gwportal | grep 'Saved but not applied'`).
