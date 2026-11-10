# gwphones

The phone platform (working name, until the brand is chosen): a hosted
phone system on Asterisk for several tenants, with their extensions,
numbers, voicemail, menus, recordings, call links, desk phones and
billing, and the operator console that runs it.

- `backend/`: the API (FastAPI). `main.py` is the sign-in check, the
  tenant switcher and the background work; the phone system is in
  `pbx.py`, and `lcr.py` (price cap and cheapest route), `billing.py`,
  `statements.py`, `recordings.py`, `ivr.py`, `hours.py` and
  `calllinks.py` beside it.
- `frontend/`: the operator console (Vite + React), all of it in
  `PbxPanel.jsx`.
- `asterisk/`: the hand-kept Asterisk files that include the generated
  ones.
- `tests/`: the tests (see Tests below).

It writes the generated Asterisk files and reloads Asterisk, so it must
run in **exactly one place**: two copies would undo each other's changes,
send voicemail emails twice and move recordings about.

## Running it

```
cd backend
python3 -m venv venv && venv/bin/pip install -r requirements.txt
GWPHONES_ALLOW_NO_AUTH=1 PBX_DISABLE_BACKGROUND=1 PBX_DB=/tmp/gw/pbx.db \
  PBX_GENERATED_DIR=/tmp/gw/gen venv/bin/uvicorn main:app --reload --port 8010
```

and `cd frontend && npm ci && npm run dev` for the console (Vite proxies
nothing: set `VITE_GWPHONES_API=http://127.0.0.1:8010` for local work).
Locally, point every `PBX_*` path at scratch folders and keep
`PBX_DISABLE_BACKGROUND=1`, so nothing touches a real Asterisk.

### Sign-in settings (`.env`)

```
GWPHONES_PROXY_SECRET=...   # nginx adds it as X-Proxy-Secret after its login
GWPHONES_TOKEN=...          # optional: a Bearer token, for scripts
GWPHONES_CORS_ORIGINS=      # normally empty: the console is served from the same site
GWPHONES_PORTAL_SECRET=...  # the tenant portal's own credential (X-Portal-Secret); empty: no portal
```

The tenant portal is a separate, much narrower caller: with
`X-Portal-Secret` it may only use the routes in `PORTAL_ROUTES`
(`main.py`), matched after routing; its tenant comes only from
`X-Portal-Tenant`, by code (1002; a suspended tenant is read-only), and `X-Portal-User`
names who's acting in `pbx-changes.log`. A request with both the
portal's and the operator's credential is refused. There are no
`/openapi.json` or `/docs` pages.

With neither secret set everything is refused, unless
`GWPHONES_ALLOW_NO_AUTH=1` (local development only). Changes (anything
but GET) from another site are refused too.

## On the server

For now it runs on the development VPS where it began (inside the admin
dashboard until November 2026), keeping that setup so nothing touching
calls, phone setup links or the route check moved: the `admindash` user
(in the `asterisk` group), port 8010, data in `/var/lib/admin-dashboard`,
logs in `/var/log/admin-dashboard`, the generated Asterisk files in
`/etc/asterisk/portal`. The code is in `/opt/gwphones`, the unit is
`gwphones.service` (see `backend/gwphones.service.example`). Those names
change once, when it moves to its own servers.

nginx, on the Tailscale-only site, behind its login:

```nginx
    location /phone-api/ {
        auth_basic "Restricted";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://127.0.0.1:8010/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Proxy-Secret "same value as GWPHONES_PROXY_SECRET";
    }

    location /phone/ {
        auth_basic "Restricted";
        auth_basic_user_file /etc/nginx/.htpasswd;
        alias /opt/gwphones/frontend/dist/;
        try_files $uri $uri/ /phone/index.html;
    }
```

and on the public site only `/prov/` and `/call/` (below).

### Deploying updates

```
cd /opt/gwphones && git pull
```

then, by what changed:

- `backend/`: `systemctl restart gwphones` (it rewrites and reloads the
  Asterisk files at start, so a change to what's generated needs only
  that).
- `backend/requirements.txt`: `cd backend && venv/bin/pip install -r requirements.txt` first.
- `frontend/`: `cd frontend && npm ci && npm run build`, then
  hard-refresh the browser.
- `asterisk/extensions.conf`: copy it over by hand and reload the
  dialplan (no calls are dropped):
  `cat asterisk/extensions.conf > /etc/asterisk/extensions.conf && asterisk -rx "dialplan reload"`,
  after restarting the service if `backend/pbx.py` changed too.

## The tenant portal (`portal/`)

A tenant's own staff managing their phone system: a separate service
(`gwportal`) with its own users database, doing every phone action
through this API as the portal caller above. Being built (phase 4).

- **Sign-in:** email and password (argon2id); two-factor codes (TOTP)
  required for tenant admins and optional for users, with recovery codes;
  the secrets encrypted with `PORTAL_KEY`.
- **Sessions:** a `__Host-` cookie (Secure, HttpOnly, SameSite=Strict),
  2 hours idle and 12 hours at most; every route says which session
  states it accepts, fully signed in by default; a new token at each step.
- **Lockouts:** an account after 5 wrong passwords or codes in a row, for
  15 minutes; an address after 20 failures in 15 minutes. The trade-off:
  anyone who knows an address can lock that account for 15 minutes at a
  time, which is why it's short, why the per-address limit does most of
  the work, and why a completed password reset clears it.
- **Links** (invitations, resets) only ever built from `PORTAL_ORIGIN`,
  with the token after `#`, so it never reaches the server's logs.
- **One process:** lockouts are in its database, but its save limit and
  Status cache are in memory, so it runs as a single uvicorn process.
- **nginx must overwrite `X-Real-IP`** with the real address
  (`proxy_set_header X-Real-IP $remote_addr;`): the portal trusts it for
  lockouts and the audit log, and only listens on 127.0.0.1.

- **Roles:** a tenant admin manages the whole tenant and its staff. A
  user has one extension, bound by its identity (number and creation
  time): they see and act on that one only, and only its recordings made
  since it was created; if it's deleted or replaced the binding ends and
  they're signed out. Users may rename it, play its recordings and change
  its forwarding, PIN, phone setup and voicemail email (setup and
  voicemail emails go to their own address only); only admins create or
  switch off extensions, invite staff and delete recordings, which takes
  confirming a warning first (`?confirm=true`; without it the answer is
  the warning). A tenant's last admin can't be demoted or disabled.
- **What gwphones says** is never passed on whole: the portal picks
  fields, keeps gwphones' messages only when they're plain answers about
  the request, and shows a save gwphones couldn't apply to Asterisk yet as
  "Saved. It takes effect in a moment." (logged as a warning: an alert to
  the operator is still to come).
- **Limits:** 20 saves a minute per tenant (each one reloads Asterisk for
  the whole server); Status and forwarding cached 5 seconds per tenant.

**The operator's command** (on the server, as the portal's user; it reads
the portal's `.env` itself):

```
venv/bin/python -m gwportal.cli invite --tenant 1002 --email anne@acme.ie --name "Anne Byrne"
venv/bin/python -m gwportal.cli invite --tenant 1002 --email anne@acme.ie --print-link
venv/bin/python -m gwportal.cli list --tenant 1002
venv/bin/python -m gwportal.cli reset-2fa --email anne@acme.ie
venv/bin/python -m gwportal.cli disable --email anne@acme.ie        (and enable)
```

`invite` makes a tenant's first admin, who then invites their own staff,
after checking with gwphones that the tenant exists and is active;
`--print-link` shows the invitation link instead of emailing it.
`reset-2fa` is for an admin who lost their phone and their recovery
codes: they set two-factor codes up again at their next sign-in.

Settings (its own `.env`): `PORTAL_DB`, `PORTAL_ORIGIN` (e.g.
`https://portal.example.ie`), `PORTAL_KEY` (a Fernet key),
`PORTAL_GW_URL`, `PORTAL_GW_SECRET` (gwphones' `GWPHONES_PORTAL_SECRET`),
`PORTAL_BRAND`, and its own copy of `MS_GRAPH_*` and
`EMAIL_FROM_ADDRESS`.

## Branches

- `main`: what runs.
- `desk-phones`: Yealink desk phones (`backend/deskphones.py`), waiting
  for a real phone to test on. It was split from the admin dashboard
  without a `main.py` of its own, so merging it needs its two routers
  added to `main.py` (`router` and `public_router`, the public one before
  pbx.py's `/prov` router); `tests/test_routes.py` fails until they are.

## Tests

```
cd backend && venv/bin/pip install -r requirements.txt -r ../portal/requirements.txt -r requirements-dev.txt
cd .. && backend/venv/bin/pytest
```

No Asterisk, real data or `.env` is needed: every test runs against
scratch folders and a fake Asterisk. `tests/golden/` holds the config
generated from a fixed set of test data (`tests/seed.py`); the golden
test fails if the generated files change at all. When a change to them
is intended, regenerate with `backend/venv/bin/python tests/seed.py
--update` and review the diff before committing.

## The phone system (Asterisk PBX)

`backend/pbx.py` serves `/api/pbx/*` and `frontend/src/PbxPanel.jsx` is
the console: live status,
extensions, outbound trunk order/caller ID/Voxbeam route, inbound routing
and ring groups.

**Busy lamps (BLF) and presence:** the portal dialplan has a hint per
enabled extension (`exten => 201,hint,PJSIP/201` in `[portal-hints]`),
and each extension's `subscribe_context` points there, so a phone or app
can watch any extension (idle, ringing, on a call) but can't reach the
rest of the dialplan through a SUBSCRIBE. Check with
`asterisk -rx "core show hints"`, whose watcher count goes up when a
phone subscribes.

**Click to call** (Phone System → Click to Call): choose your extension
and a number. The service runs `channel originate
Local/<ext><number>@portal-c2c-ring/n extension <ext><number>@portal-c2c-dial`:
your extension rings showing the number (even on do not disturb), and
once you answer, the number is dialled through from-internal as that
extension (its caller ID, the route check and price cap, billed to it via
CDR userfield `c2c:<ext>`). Both contexts are generated and included
nowhere a phone can dial. The number is checked first (digits only,
never 112/999), and the page shows the route and price, or why it would
be refused, before you press Call. "Open tel: links here" makes the
console the browser's handler for `tel:` links (Chrome, Edge, Firefox):
a link opens `/phone/?call=...` with the number filled in, and nothing
is dialled until you press Call.

**Do not disturb, call forwarding and pickup.** Calls for an extension
(from another extension, an inbound route or a forward) go from the
static `_2XX` to the generated `[portal-call-ext]`: forward always, else
do not disturb (counts as busy), else ring 30 s; busy goes to the busy
forward, anything else unanswered (including offline) to the no-answer
forward; with no forward, voicemail. Ring groups skip members on do not
disturb. Settings live in AstDB (families `DND`, `CF`, `CFB`, `CFU`, as
FreePBX uses them), so the phones' feature codes and the service
(Phone setup → Call forwarding) change the same thing:

| Code | |
|---|---|
| `*78` / `*79` | Do not disturb on / off |
| `*72<number>` / `*73` | Forward all calls / stop |
| `*90<number>` / `*91` | Forward when busy / stop |
| `*52<number>` / `*53` | Forward when unanswered or offline / stop |
| `*8` | Answer any ringing extension (every extension is in pickup group `portal`) |
| `**2XX` | Answer that extension's ringing call |

Forwards go only to other extensions or Irish landlines and mobiles in
national format, checked in the service and again in the dialplan:
anyone who rings your number can set a forward going, so never abroad,
1800/premium or 112/999. A forward to an outside number dials out like
any call (route check, price cap), shows the forwarding extension's own
caller ID (`EXTCID_<ext>` in `[globals]`) or the default, and has no
voicemail after it. If the caller was outside, the call is billed to the
forwarding extension (CDR userfield `fwd:<ext>`); an extension's own call
that got forwarded stays its own. Loops stop after 3 forwards, at
voicemail. Deleting an extension clears its settings and forwards to it.
Do not disturb doesn't show on busy lamps.

**Video calls between extensions** (switch at the top of Extensions, off
by default) adds `allow=h264,vp8` to each generated extension, on top of
the audio codecs in the hand-edited `endpoint-ext` template. Asterisk
passes the video through without transcoding, so both phones need a
codec in common (Groundwire and Linphone both do H.264 and VP8; H.264
is first because iPhones encode it in hardware, VP8 in software). Trunk
calls stay audio only, since the trunks don't allow video.

Each extension can have its own outbound caller ID (under Phone setup).
It's written as `set_var=EXT_CID=...` on the extension's endpoint, and
`[sub-outbound]` plus 112/999 in `extensions.conf` use it, falling back to
the default `OUTBOUND_CID` when it's blank.

How it works: the service keeps its own `pbx.db` (SQLite, mode 600,
holds the SIP passwords) and on every save regenerates two files and
reloads Asterisk. Both live in `/etc/asterisk/portal`
(`PBX_GENERATED_DIR`), so the service can't change anything else in
`/etc/asterisk`:

- `portal/pjsip_extensions.conf` — extensions, included from `pjsip.conf`
  with `#include portal/pjsip_extensions.conf`
- `portal/extensions_portal.conf` — `[globals]`, ring groups,
  outbound trunk order, inbound routes
- `portal/voicemail_portal.conf` — a mailbox (PIN) per extension, in the
  voicemail context `portal`, included from `voicemail.conf` with
  `#include portal/voicemail_portal.conf`

`asterisk/extensions.conf` in this repo is the static dialplan that
includes the second file; it replaces `/etc/asterisk/extensions.conf`.
It sorts dialled numbers into Irish mobiles, other Irish numbers,
freephone and international, and the **Outbound trunks** section decides
which trunk each kind uses (the trunk order, one trunk only, or blocked).
Defaults: mobiles and freephone via Irish VoIP only, other Irish numbers
by the trunk order, international via Voxbeam only. 112/999 always go via
Irish VoIP.
Transports and the trunk definitions in `pjsip.conf` stay hand-edited.
On first start, existing extensions and their passwords are imported from
the current `pjsip_extensions.conf`, so no phone needs re-entering.

Optional `.env` settings:

```
PBX_PUBLIC_HOST=pbx.amapforyourmoney.com   # shown on the phone setup card
PBX_PUBLIC_PORT=15061
PBX_TENANT_HOST_SUFFIX=.sip.amapforyourmoney.com   # tenants' own SIP hostnames are one name under this
PBX_DB=/var/lib/admin-dashboard/pbx.db
PBX_ASTERISK_DIR=/etc/asterisk
PBX_GENERATED_DIR=/etc/asterisk/portal     # set in the example unit
PBX_CHANGES_LOG=/var/log/admin-dashboard/pbx-changes.log
PBX_LCR_URL=http://127.0.0.1:8010/lcr/route   # where Asterisk asks for routes: this service's own address
PBX_ALLOW_NEW_DB=1          # only on purpose: start from a new, empty pbx.db although a config was generated
PBX_DISABLE_BACKGROUND=1    # only for a test run against copies: no config rewrite at start, voicemail
                            # emails, recording clean-up or billing imports
```

If `pbx.db` is missing while `PBX_GENERATED_DIR` already holds a generated
config, the service refuses to create a new, empty one (and says so in
`pbx-changes.log`): applying from it would rewrite the live config with no
extensions. That catches a wrong `PBX_DB` path after a move. Set
`PBX_ALLOW_NEW_DB=1` only when starting afresh is really meant.

### Voicemail

Calls to an extension that aren't answered within 30 seconds, or reach a
busy or offline phone, go to that extension's voicemail. A ring group can
send its unanswered calls to one extension's mailbox (set on the group).
Dial **\*97** to check your own voicemail and **\*98** for any mailbox; both
ask for the PIN, which is on the extension's setup card (**New voicemail
PIN** changes it; changing it from the phone's voicemail menu isn't
supported, as the next save would overwrite it). Phones get a
message-waiting indicator. Needs Asterisk's voicemail module
(`apt install asterisk-voicemail` on Debian/Ubuntu), and only the plain
one: Ubuntu's package can start the IMAP version instead, which leaves
no `VoiceMail` application ("No application 'VoiceMail'" in the log).
`module show like voicemail` should show `app_voicemail.so` Running. If
it doesn't, add these lines straight under `[modules]` in
`/etc/asterisk/modules.conf` (not at the end of the file, which is in
`[global]`) and restart Asterisk:

```
noload => app_voicemail_imap.so
noload => app_voicemail_odbc.so
```

### Phone setup by QR code (Groundwire, Linphone)

**Phone setup → QR code for Groundwire / Linphone** makes a one-time link
and shows it as a QR code, so nobody types the server, port, TLS, SRTP or
password:

- **Groundwire:** the iPhone's Camera opens the link in Safari, which
  hands it to Groundwire (`provlinkbs://`), and Groundwire fetches
  `/prov/<token>/account.xml` (Acrobits Account XML, with push). The
  settings only go to a client that calls itself Groundwire or Acrobits.
- **Linphone:** the QR code is `/prov/<token>/linphone.xml` itself (a
  Linphone remote-provisioning file), scanned with **Scan QR code** on
  Linphone's setup screen (Linphone has no paste-a-link option). Opened in
  a browser instead (the Camera app, or a link in the setup email), it
  shows a page with **Open in Linphone** (a `linphone-config:` link) and
  never the password. The settings themselves only go to a client that
  calls itself Linphone, so an email scanner following the QR code can't
  use the link up. Linphone gets no push from Asterisk, so it
  won't ring while it's closed.

- A link lasts 15 minutes and stops working a minute after the phone first
  fetches the settings. A new password, or turning off or deleting the
  extension, cancels it. Only a hash of each token is kept in `pbx.db`.
- Opening the link in a browser only shows a hand-over page; it doesn't
  use the link up.
- Each link made and each settings fetch (with the phone's IP) go in
  `pbx-changes.log`.

The phone isn't on Tailscale, so `/prov/` must be reachable from the
internet, and it is the **only** part of this service that may be. In the
public site's server block (e.g. `amapforyourmoney.com`):

```nginx
    # Phone setup links only. Never add X-Admin-Proxy-Secret here, and
    # never widen this location: that would put the whole API online.
    location /prov/ {
        proxy_pass http://127.0.0.1:8010/prov/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        access_log off;   # the token is in the URL
    }
```

Then in `.env`, the public address of that site:

```
PBX_PROVISION_BASE_URL=https://amapforyourmoney.com
```

**Email setup** on the same card sends the client a link to tap on the
iPhone, QR codes for reading the email on a computer, and the voicemail
PIN. Emailed links work once, for 72 hours (`PBX_EMAIL_LINK_HOURS`). The
email goes through Microsoft Graph, with these settings in the service's
`.env`:

```
MS_GRAPH_TENANT_ID=...
MS_GRAPH_CLIENT_ID=...
MS_GRAPH_CLIENT_SECRET=...
EMAIL_FROM_ADDRESS=...
```

`segno` (in `requirements.txt`) draws the QR code: run
`venv/bin/pip install -r requirements.txt` after pulling.

### Voicemail by email

Give an extension an address under **Email new voicemails to** on its
setup card, and each new message is emailed there with the recording
attached (up to 2.5 MB, which Graph's size limit allows; longer ones say
to dial \*97). Messages also stay in the mailbox.

Asterisk writes the email itself, but there's no mail server on this VPS,
so its `mailcmd` is `asterisk/vm-mail.sh`, which only drops the email in
`/var/spool/asterisk/vm-outbox` (`PBX_VM_OUTBOX`). The service checks
that folder every 15 seconds and sends each email through Microsoft
Graph, but only to an address that is set on a mailbox. One-off setup:

```
install -m 755 asterisk/vm-mail.sh /usr/local/bin/asterisk-vm-mail
install -d -o asterisk -g asterisk -m 2770 /var/spool/asterisk/vm-outbox
```

and in the `[general]` section of `/etc/asterisk/voicemail.conf`:

```
mailcmd=/usr/local/bin/asterisk-vm-mail
attach=yes
emailbody=Mailbox: ${VM_MAILBOX}\nCaller-Name: ${VM_CIDNAME}\nCaller-Number: ${VM_CIDNUM}\nLength: ${VM_DUR}\nWhen: ${VM_DATE}\n
```

`emailbody` makes Asterisk write the caller, mailbox, length and time as
lines the service reads; its default text has no fixed format (and its
`X-Asterisk-VM-*` headers only come with IMAP storage), so without it the
email says "Unknown caller".

The service unit must allow writing to the outbox
(`ReadWritePaths=... -/var/spool/asterisk/vm-outbox`, in the example
unit). If Graph is down, emails wait in the outbox and are retried; after
a day they're dropped and logged in `pbx-changes.log`.

### Call links (`backend/calllinks.py`)

A call link is a URL anyone can open to call an extension or ring group
from their browser (WebRTC, no app or account), for an email signature,
a website button or a QR code. Phone System → Call Links makes them, with
the link, QR code and HTML snippets; each can allow video and has a
longest call. New URL replaces a link everywhere it's been shared.

How it fits together:

- The page `/call/<token>` (public, served by the service) carries the
  login for one guest endpoint, `webguest`, so that login is public. The
  endpoint can only reach the generated `[portal-weblinks]`, which has one
  entry per live link (`wl-<token>`): an unknown token is a 404, and there
  is no way from there to a trunk. The endpoint is only generated while a
  link is live, has no AOR (can't register), and can't subscribe or
  transfer.
- The browser loads JsSIP (`backend/static/call/`, bundled, MIT; see
  `THIRD-PARTY.txt`) from the service only, and opens a SIP WebSocket to
  `wss://<public site>/pbx-ws`, which nginx proxies to Asterisk on
  127.0.0.1:8088. Audio (and video) goes straight to Asterisk's public
  address on its usual RTP ports, DTLS-SRTP encrypted. No STUN server: the
  PBX has a public address.
- Limits: 2 calls at once per link and 5 in all, and the link's longest
  call (dialplan), plus requests per IP in nginx, since Asterisk sees
  every browser as 127.0.0.1. The page never calls until Call is pressed.
- On the phone the call shows as "Web <name> (<link name>)", number `web`.
  It goes to the extension like any call, so do not disturb, forwarding
  and voicemail apply (a forward to a mobile places a paid call, billed
  to the forwarding extension, as for an outside caller).
- Video needs the link's video option and video calls turned on under
  Extensions.

One-time setup on the VPS:

1. `/etc/asterisk/http.conf`: Asterisk's web server, on this machine only:

   ```ini
   [general]
   enabled=yes
   bindaddr=127.0.0.1
   bindport=8088
   ```

2. `/etc/asterisk/ari.conf`: nothing uses ARI, so `enabled = no`.

3. Add to `/etc/asterisk/pjsip.conf`, then `asterisk -rx "core restart when convenient"`:

   ```ini
   [transport-ws]
   type=transport
   protocol=ws
   bind=0.0.0.0
   ```

4. nginx: `/etc/nginx/conf.d/pbx-limits.conf`:

   ```nginx
   limit_conn_zone $binary_remote_addr zone=pbxws:10m;
   limit_req_zone $binary_remote_addr zone=pbxcall:10m rate=20r/m;
   ```

   and in the public site's server block, next to `/prov/`:

   ```nginx
       # Call link pages. Never add X-Admin-Proxy-Secret here.
       location /call/ {
           limit_req zone=pbxcall burst=20 nodelay;
           proxy_pass http://127.0.0.1:8010/call/;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           access_log off;   # the link is in the URL
       }

       # The browsers' SIP WebSocket: Asterisk's /ws and nothing else.
       location = /pbx-ws {
           limit_conn pbxws 4;
           proxy_pass http://127.0.0.1:8088/ws;
           proxy_http_version 1.1;
           proxy_set_header Upgrade $http_upgrade;
           proxy_set_header Connection "upgrade";
           proxy_set_header Host $host;
           proxy_read_timeout 3h;   # a quiet call must not drop the socket
           access_log off;
       }
   ```

   If that server block sets `X-Frame-Options` or a Content-Security-Policy
   with `add_header`, the built-in (iframe) snippet won't work there; the
   link and button always do.

Check: `asterisk -rx "http show status"` says enabled on 127.0.0.1:8088,
`asterisk -rx "pjsip show transports"` lists `transport-ws`, and opening
the WebSocket through nginx answers `101 Switching Protocols`.

If chan_sip is loaded, `res_pjsip_transport_websocket` "declined to load"
and every PJSIP transport disappears: make sure `noload => chan_sip.so`
is under `[modules]` in `/etc/asterisk/modules.conf` (not further down,
in another section), then `asterisk -rx "core restart now"`.

### IVR menus (`backend/ivr.py`)

"Press 1 for sales, 2 for support". Each menu has a number (500-599; 7XX
is usually Asterisk's parking lot), a greeting, a target per key (an extension, a
ring group, another menu, an extension's voicemail, or hang up), how long
to wait for a key, how many times to play the greeting, and where the call
goes after that (or goodbye). Optionally callers can dial an extension
straight from the menu. Inbound numbers, the default destination, call
links (which then show a keypad) and phones can all ring a menu.

Each menu is its own context, `[portal-ivr-5XX]`, entered at `s`, with
no includes: browsers reach menus through call links, so a menu offers
only what's listed in it. The menu numbers themselves are in
`[portal-ringgroups]`, which `from-internal` already includes, so the
static `extensions.conf` doesn't change. A call that goes through more
than 20 menus (a fallback loop) is hung up.

Greetings are converted in the service page, from an uploaded file or a
recording from the browser's microphone, to 8 kHz mu-law with no header,
and stored as `/etc/asterisk/portal/ivr/t<tenant code>/ivr-<number>.ulaw`
(`t1001` for the main tenant; the folder the service already writes,
whose setgid bit gives the files to the `asterisk` group). The server never reads an uploaded audio file and
needs no sox. Up to 2 minutes; the Tailscale site's
`client_max_body_size 30m` (see Billing) covers that. A menu with no
greeting plays a beep.

The menus also use three of Asterisk's own sounds, `beep`, `invalid`
and `vm-goodbye`, from the core sounds package:
`ls /usr/share/asterisk/sounds/en | grep -E "^(beep|invalid|vm-goodbye)\."`

### Call park

Asterisk's own parking lot, `res_parking.conf` left at its defaults: 700
parks (transfer a call to it; the slot is read out), 701-720 pick a
parked call up, and after 45 s an unanswered parked call rings the phone
that parked it for 30 s. The generated `[portal-features]` includes
`parkedcalls`, so only phones reach it (not browsers or menus), and
`[portal-hints]` has `park:70X@default` hints for BLF keys on the slots.

Every other tenant gets a lot of its own with the same numbers
(`t<code>-park`, context `t<code>-parkedcalls`, which only its feature codes
include), generated into `portal/res_parking_portal.conf`. Include it once,
at the end of `/etc/asterisk/res_parking.conf`:

    #include portal/res_parking_portal.conf

then `asterisk -rx "module reload res_parking.so"`. The service reloads
parking itself after that, but only when a lot is added or removed, never
on an everyday save.

### Call recording (`backend/recordings.py`)

Two ways: "Record every call" on an extension (every answered call it
makes or takes, internal ones included, and ring group calls it answers),
or **Record** next to a call in progress on the Status page (from then
on). Asterisk records with MixMonitor, only while the two are connected,
as 8 kHz mu-law (about 0.5 MB a minute) into the tenant's folder,
`/var/spool/asterisk/monitor/portal/t<tenant code>` (`PBX_RECORDINGS_DIR`,
then `t1001` for the main tenant; the service makes each one), named
`<time>_<ext>_<other party>_<in|out|int|req>_<uniqueid>.ulaw`. The
Recordings page lists them, plays and downloads them as WAV and deletes
them; anything older than the days set there (90 by default) is deleted
every hour.

How: each `Dial()` to an extension, ring group or trunk has option
`U(portal-rec)`, which runs `[portal-rec]` on the channel that answers.
It records when that extension (`REC_<login>` in `[globals]`) or the one
that started the call (`RECEXT`, inherited) records everything, into the
call's tenant's folder (`RECDIR_<code>`).

The notice ("calls may be recorded"), uploaded or recorded like an IVR
greeting, is stored as `portal/ivr/t<tenant code>/recording-notice.ulaw`. While any
extension records every call, outside callers hear it before they're put
through, and an outside number you call hears it when it answers, before
you're connected. The Record button doesn't announce anything.

One-time setup, a folder both Asterisk and the service can use:

```
sudo install -d -o asterisk -g asterisk -m 2770 /var/spool/asterisk/monitor/portal
```

and add `-/var/spool/asterisk/monitor/portal` to `ReadWritePaths` in the
service (see the example unit), then `sudo systemctl daemon-reload` and
restart it. `asterisk -rx "module show like mixmonitor"` should say
Running.

Until phase 3 of the multi-tenant work, the main tenant's recordings,
greetings and notice sat directly in these folders. The first save after
updating moves its recordings into `t1001` (any started in that moment
follow at the hourly clean-up) and copies its greetings and notice there,
removing the old ones once Asterisk has reloaded.

### Business hours (`backend/hours.py`)

A schedule (400-499) sends calls to one place when open and another when
closed, e.g. the main menu by day and voicemail at night. Each day can
have up to four openings (around lunch, say); dates added as closed (bank
holidays) are closed all day, and past ones are dropped on save. "Close
now" / "Open now" overrides the clock until it's switched back. Inbound
numbers, call links, IVR keys and phones can ring a schedule; a
schedule's own open/closed targets can't be another schedule (nothing
would stop a loop).

Times are in `PBX_TIMEZONE` (default Europe/Dublin), passed to Asterisk
explicitly, so the VPS's own clock zone doesn't matter. Each schedule is
`[portal-hours-4XX]`: it compares `${STRFTIME(${EPOCH},<zone>,%u%H%M)}`
(Monday 09:30 is 10930) with each opening as numbers, start included, end
not, which is the same rule the service uses for its open/closed badge.

### Price cap and cheapest route (`backend/lcr.py`)

Before dialling any outbound call (all but 112/999), the generated
dialplan asks the service which routes to try:
`GET http://127.0.0.1:8010/lcr/route?kind=<number type>&num=<number>` with
an `X-LCR-Token` header (`LCR_TOKEN` in `[globals]`, made once and kept
in `pbx.db`), using Asterisk's `CURL()` (`func_curl` must be loaded),
with 1 s connect and 2 s response timeouts. It answers only this machine,
not requests through nginx; never add `/lcr/` to an nginx site. The
answer is `OK:irishvoip/0011103` (routes to try in order: a trunk, or a
Voxbeam route prefix) or `REFUSE:<reason>`.

- Candidates: the number type's setting in **Outbound trunks**: the trunk
  order, one trunk, or **Cheapest route** (every route ticked under "Routes
  Cheapest route may use", ordered by the cost of a typical 2-minute call,
  so increments and connection fees count).
- Whatever the setting, a route is dropped if its rate sheet has no price
  for the number or its per-minute price is over the **cap** (€ ex VAT; a
  default, optionally one per number type). None left: the call is
  refused. The prices come from the same lookup billing uses, with the
  newest exchange rate already fetched (billing keeps it fresh).
- A route whose newest rate sheet has expired keeps its latest prices, so
  calls don't fail, but the cap can't see a price rise until the newer
  sheet is uploaded: the Trunks section shows a warning until it is.
- **No answer** from the service (restarting, down): Irish numbers go out
  on the trunk order, unchecked; international calls are refused. Keep a
  high safety cap on Voxbeam's portal for that case.
- **Coverage** shows, per number type, every destination in the current
  rate sheets as available, partly or unavailable under the saved routing,
  ticks and caps (the same rules as real calls), with the route and price,
  or the cheapest blocked price and route against the cap; each column
  can be filtered (prefix searches every prefix), and it downloads per
  prefix as CSV. Irish ranges (083…089, 1800) are
  checked as themselves even where a sheet has only a broader prefix.
- **Test a number** (below the trunk settings) shows what a call would do
  and every route's price; it also lists the last 50 real checks and warns
  if calls went out without one.

### Billing (calls and costs)

**Phone System → Billing** reads Asterisk's call log
(`/var/log/asterisk/cdr-csv/Master.csv`, `PBX_CDR_CSV`) every minute into
`/var/lib/admin-dashboard/billing.db` (`PBX_BILLING_DB`, mode 600) and
prices each answered outbound call:

- **Rate sheets**: upload each provider's own spreadsheet (CSV,
  tab-separated or Excel, also zipped or gzipped as they're sent), one per
  Voxbeam route. The columns are guessed
  from the headers, shown for checking, and remembered per provider. Every
  upload is kept; a call is priced from the sheet in force when it was
  made, with a row's "existing rate" before its change date. Rows marked
  deleted or removed are skipped.
- **Pricing**: the longest matching prefix, that row's billing increments
  (1/1 per second, 60/60, 30/6) and connection fee, then euro at the ECB
  rate for the call's day (Frankfurter). Each call keeps
  its pricing trace (click it).
- **Never zero by accident**: a call that can't be priced (no sheet for
  its route, no matching prefix, no exchange rate yet) shows as *Not
  priced* with the reason; one after its rate's expiry is *Provisional*
  until a newer sheet is uploaded. Outbound calls that didn't connect are
  *Not connected* (no answer, busy, failed, congestion); internal, inbound
  and 112/999 calls are free. Neither is ever billed.
- The extension comes from the channel, not `src` (that's the caller ID
  shown); the trunk, Voxbeam route and real number from `lastdata`.
  Irish numbers become `353…` (1800 stays Irish, not North American).
- CDR times are read as `PBX_CDR_TIMEZONE` (default UTC, as Asterisk
  writes them here) and shown and grouped into months in `PBX_TIMEZONE`
  (default Europe/Dublin).

**Customers** group extensions; each pays your cost plus a markup (the
default, or their own) for their priced calls and for the **phone
numbers** (DDIs) assigned to them, which have a monthly cost ex VAT (or a
fixed monthly price) and the months they're active. **What each customer
owes** shows, per month, calls, minutes, cost, price and margin, flags a
customer with calls not priced yet (left out of the totals until they
are), and downloads as CSV, overall or itemised per customer. Calls from
extensions no customer has are under "Not assigned". Calls follow an
extension's current customer until they're on an issued statement.

**Statements** (`backend/statements.py`): once a month has ended, issue a
statement for one customer or all of them. Issuing locks it: its calls
get their price written down and its phone numbers are marked billed for
the month, so a later rate sheet or markup change never alters it, and
what it shows is kept as a snapshot (the PDF is always the same
document). A customer with calls not priced, or only provisionally
priced, can't be issued (a bulk run is all or nothing). Calls read after
a statement go on a follow-up for the same month. **Cancel** unlocks a
statement's calls and numbers for re-issuing; the cancelled statement and
its number (S00001, S00002, …) stay on record. The PDF (ReportLab) has the
business details (**Business details**: name, address, contact, how to
pay, footer), a summary, phone numbers, calls by destination and
itemised calls. **Email** sends it through Microsoft Graph to the
customer's address. **Preview** gives a draft PDF for any month, the
current one too: marked DRAFT, unnumbered, nothing locked, unpriced calls
left out and counted. These are statements, not VAT invoices ("VAT not
charged"); the totals keep a VAT line, zero for now.

Rate sheets are large, so the Tailscale nginx site needs a bigger upload
limit in its `/admin-api/` location:

```nginx
        client_max_body_size 30m;
```

### PBX certificate

The phones' TLS certificate is Let's Encrypt's, for `PBX_PUBLIC_HOST` and
the tenants' hostnames, `*.sip.amapforyourmoney.com` (one wildcard name,
so a new tenant's hostname needs no new certificate; the DNS wildcard
already points it here). A wildcard can only be issued by a DNS check, so
certbot uses its Cloudflare plugin with a token limited to this zone and
to the VPS's addresses, in `/etc/letsencrypt/cloudflare.ini` (root only):

```
certbot certonly --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  --cert-name pbx.amapforyourmoney.com -d pbx.amapforyourmoney.com -d '*.sip.amapforyourmoney.com'
```

A tenant's phones are set up to register to its own hostname (Tenants, SIP
hostname), so it can later be moved to another server by an A record for
that name, which overrides the wildcard. Phones already set up keep the
hostname they were given until they're set up again.

Certbot renews it and runs
`/etc/letsencrypt/renewal-hooks/deploy/asterisk-cert.sh`, which copies it
to `/etc/asterisk/keys/pbx.crt` and `pbx.key` (owner asterisk, mode 600)
and then runs `asterisk -rx "core restart when convenient"`: Asterisk
restarts once no calls are up, so a renewal never drops a call. (A
`pjsip reload` isn't enough: it doesn't reload a TLS transport whose
settings are unchanged, so Asterisk would keep the old certificate.) The
restart takes about 20 seconds; phones re-register by themselves. To test
the hook without a renewal:

```
