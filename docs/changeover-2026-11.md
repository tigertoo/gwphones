# Changeover: the phone system moves from admin-dashboard to gwphones

The evening runbook for the development VPS (step 5 of the split; see the
design doc, "Splitting the phone platform into its own repository").
About 30 minutes, at a quiet time, **well before the 02:00 backup**. Run
everything as root. Don't reboot during it.

Calls are never interrupted: Asterisk runs throughout. For the minute or
so between stopping the old service and the new one answering, phone setup
links, call link pages and the route checks are down; Irish calls out
still go (the dialplan falls back), international ones are refused.

## Done beforehand (part A, 10 November 2026)

- `/opt/gwphones` cloned, venv built, 21 tests passing, console built.
- `/opt/gwphones/backend/.env`: the phone settings from the old `.env`
  (one `PBX_PROVISION_BASE_URL`) plus a new `GWPHONES_PROXY_SECRET`.
- Real data through the new code: all four Asterisk files identical.
- User `vpsdash`, `/var/lib/vps-dashboard` (700), `/var/log/vps-dashboard`.
- `gwphones.service` installed, disabled, inactive.
- admin-dashboard's `phone-split` branch (e7b52aa) pushed; `main` untouched.

## B0. On your own machine: merge the dashboard's branch

```bash
cd ~/dev/admin-dashboard && git switch main && git merge --ff-only phone-split && git push
```

Expect a fast-forward to `e7b52aa`. (The server doesn't pull it until B5.)

## B1. Rollback point

```bash
umask 022
mkdir -p /root/split-rollback
cp -a /etc/asterisk/portal /root/split-rollback/portal
cp -a /var/lib/admin-dashboard /root/split-rollback/data
cp /etc/systemd/system/admin-dashboard.service /root/split-rollback/admin-dashboard.service
cp /etc/nginx/sites-available/admin-tailscale /root/split-rollback/admin-tailscale
cp -a /opt/admin-dashboard/frontend/dist /root/split-rollback/dashboard-dist
git -C /opt/admin-dashboard rev-parse HEAD > /root/split-rollback/admin-dashboard.commit
# The dashboard's new unit, staged without touching its working tree
git -C /opt/admin-dashboard fetch -q
git -C /opt/admin-dashboard show origin/main:backend/admin-dashboard.service.example > /root/split-rollback/admin-dashboard.service.new
grep -E '^(User|ExecStart)=' /root/split-rollback/admin-dashboard.service.new
```

Expect `User=vpsdash` and `--port 8011`.

## B2. The switch: one line

```bash
systemctl stop admin-dashboard && cp /root/split-rollback/admin-dashboard.service.new /etc/systemd/system/admin-dashboard.service && systemctl daemon-reload && systemctl enable --now gwphones
```

From here the old unit can only ever start as the slim dashboard
(`vpsdash`, port 8011, no access to the phone data), even by accident.
Don't start `admin-dashboard` yet.

## B3. Phone checkpoint: go or no-go

```bash
sleep 20
systemctl is-active gwphones
ss -ltnp | grep -E ':801[01]\b'
curl -s -o /dev/null -w 'api: %{http_code}\n' -H "X-Proxy-Secret: $(sed -n 's/^GWPHONES_PROXY_SECRET=//p' /opt/gwphones/backend/.env)" http://127.0.0.1:8010/api/pbx/config
diff -rq /root/split-rollback/portal /etc/asterisk/portal && echo "portal files UNCHANGED"
tail -2 /var/log/admin-dashboard/pbx-changes.log
journalctl -u gwphones -n 30 --no-pager | grep -iE 'traceback|error' || echo "no errors in the log"
```

Expect: `active`; **one** listener, on 8010; `api: 200`;
`portal files UNCHANGED`; a `Dashboard started: rewrote and reloaded` line
(gwphones keeps the old wording); `no errors in the log`.

Then **a test call out to a mobile** from Groundwire or Linphone (proves
the route check answers on 8010).

**Any of these wrong: roll back the phone half (R1) now.**

## B4. The dashboard's ideas, before it ever starts

```bash
cp -a /var/lib/admin-dashboard/ideas.db /var/lib/vps-dashboard/ideas.db
chown vpsdash:vpsdash /var/lib/vps-dashboard/ideas.db && chmod 600 /var/lib/vps-dashboard/ideas.db
python3 -c "import sqlite3; [print(p, sqlite3.connect(p).execute('SELECT COUNT(*) FROM ideas').fetchone()[0]) for p in ('/var/lib/admin-dashboard/ideas.db', '/var/lib/vps-dashboard/ideas.db')]"
```

Expect the same count twice.

## B5. The slim dashboard

```bash
cd /opt/admin-dashboard && git pull --ff-only && git log --oneline -1
cp /opt/admin-dashboard/backup-vital-files.sh /root/backup-vital-files.sh
cd /opt/admin-dashboard/frontend && npm ci --no-audit --no-fund 2>&1 | tail -1 && npm run build 2>&1 | grep -E 'built|rror'
systemctl start admin-dashboard && sleep 5
systemctl is-active admin-dashboard
curl -s -o /dev/null -w 'dashboard health: %{http_code}\n' http://127.0.0.1:8011/api/health
ss -ltnp | grep -E ':801[01]\b'
```

Expect `e7b52aa`, `built`, `active`, `dashboard health: 200`, and now
**two** listeners: gwphones on 8010, the dashboard on 8011.

## B6. nginx: both changes in one reload

`/admin-api/` goes to 8011, and `/phone-api/` and `/phone/` are added with
gwphones' secret. The script checks the file first and changes nothing
unless each anchor is found exactly once; it never prints the file, keeps
it at mode 600, and puts the old one back if `nginx -t` fails.

```bash
python3 - <<'EOF'
import os, re, shutil, subprocess, sys
site = "/etc/nginx/sites-available/admin-tailscale"
secret = [l.split("=", 1)[1].strip() for l in open("/opt/gwphones/backend/.env") if l.startswith("GWPHONES_PROXY_SECRET=")][0]
s = open(site).read()
if "location /phone/" in s:
    sys.exit("already has /phone/: nothing done")
old_api = "proxy_pass http://127.0.0.1:8010/;"
catch_all = re.compile(r"\n([ \t]*)location / \{ return 404; \}")
if s.count(old_api) != 1 or len(catch_all.findall(s)) != 1:
    sys.exit(f"anchors not found exactly once ({s.count(old_api)}, {len(catch_all.findall(s))}): nothing done")
shutil.copy2(site, "/root/split-rollback/admin-tailscale.before-b6")
s = s.replace(old_api, "proxy_pass http://127.0.0.1:8011/;")
def phone(m):
    i = m.group(1)
    return (f"\n{i}# The phone system's console (gwphones, port 8010)\n"
            f"{i}location /phone-api/ {{\n"
            f"{i}    auth_basic \"Restricted\";\n"
            f"{i}    auth_basic_user_file /etc/nginx/.htpasswd;\n"
            f"{i}    proxy_pass http://127.0.0.1:8010/;\n"
            f"{i}    proxy_set_header Host $host;\n"
            f"{i}    proxy_set_header X-Real-IP $remote_addr;\n"
            f"{i}    proxy_set_header X-Proxy-Secret \"{secret}\";\n"
            f"{i}}}\n\n"
            f"{i}location /phone/ {{\n"
            f"{i}    auth_basic \"Restricted\";\n"
            f"{i}    auth_basic_user_file /etc/nginx/.htpasswd;\n"
            f"{i}    alias /opt/gwphones/frontend/dist/;\n"
            f"{i}    try_files $uri $uri/ /phone/index.html;\n"
            f"{i}}}\n" + m.group(0))
s = catch_all.sub(phone, s, count=1)
tmp = site + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    f.write(s)
os.replace(tmp, site)
t = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
if t.returncode:
    shutil.copy2("/root/split-rollback/admin-tailscale.before-b6", site)
    sys.exit("nginx -t failed, old file put back:\n" + t.stderr)
subprocess.run(["systemctl", "reload", "nginx"], check=True)
print("nginx updated and reloaded")
EOF
stat -c '%a %n' /etc/nginx/sites-available/admin-tailscale
```

Expect `nginx updated and reloaded` and `600`.

## B7. In the browser (close any old Phone System tabs on /admin/ first)

1. `/admin/`: vitals and Money Mapper load; the **Phone System ↗** tab
   opens `/phone/`.
2. `/phone/`: Status shows the trunks and phones; **Re-apply**: files
   unchanged.
3. A call out to a mobile.
4. Ring your number from a mobile, leave a voicemail: the **voicemail
   email** arrives (gwphones sends them now).
5. Make a **QR setup link** for an extension and open it (public `/prov/`).
6. Open a **call link** page (public `/call/`), if you have one.
7. Optional: in `/phone/`, "Open tel: links here" again, so the browser
   uses the new address directly (old `/admin/?call=` links are passed on
   anyway).

## The next morning

```bash
grep -E 'WARNING|complete' /root/backup.log | tail -3
tar -tzf "$(ls -t /root/backups/finance-app-backup_*.tar.gz | head -1)" | grep -E 'gwphones|pbx.db|ideas.db'
```

Expect no WARNING, "Backup complete", and `gwphones.env`,
`gwphones.service`, `pbx.db` and `ideas.db` in the archive. A reboot some
time later is a good test: both come up, and only gwphones writes the
Asterisk folder.

## Rollback

The data paths are shared, so anything saved in the new console before a
rollback is kept.

**R1. Phone half only** (B3 failed; the dashboard code not pulled yet):

```bash
systemctl disable --now gwphones
cp /root/split-rollback/admin-dashboard.service /etc/systemd/system/admin-dashboard.service
systemctl daemon-reload && systemctl start admin-dashboard
sleep 15 && diff -rq /root/split-rollback/portal /etc/asterisk/portal && echo "back as before"
```

**R2. Everything** (after B5 or B6):

```bash
systemctl disable --now gwphones
systemctl stop admin-dashboard
git -C /opt/admin-dashboard checkout -q "$(cat /root/split-rollback/admin-dashboard.commit)"
rm -rf /opt/admin-dashboard/frontend/dist && cp -a /root/split-rollback/dashboard-dist /opt/admin-dashboard/frontend/dist
cp /root/split-rollback/admin-dashboard.service /etc/systemd/system/admin-dashboard.service
cp /root/split-rollback/admin-tailscale /etc/nginx/sites-available/admin-tailscale
systemctl daemon-reload && nginx -t && systemctl reload nginx && systemctl start admin-dashboard
```

That checkout is detached: before the next normal pull,
`git -C /opt/admin-dashboard checkout main`.

## A week later (step 6)

Remove `/root/split-rollback`, `/root/gwcheck`, the old
`/var/lib/admin-dashboard/ideas.db`, and the `PBX_*`, `MS_GRAPH_*` and
`EMAIL_FROM_ADDRESS` lines from `/opt/admin-dashboard/backend/.env`;
delete the `desk-phones` branch from admin-dashboard (it lives on in
gwphones); update the notes.
