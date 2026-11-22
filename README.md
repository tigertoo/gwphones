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
        client_max_body_size 110m;   # rate sheets, greetings, desk-phone firmware
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
- **Trusted devices:** at the code step, "Don't ask for a code on this
  device for 30 days" (off by default; admins too). A random token in a
  second cookie, `__Host-gwportal-device` (same flags, 30 days), only its
  hash stored, and only for the account that chose it: the password is
  always needed. Fixed 30 days from when it was chosen, not renewed by
  use. Changing or resetting the password, any change to two-factor
  codes, `reset-2fa` or `disable` forgets every trusted device; the
  Account page shows how many there are and forgets this one or all.
- **Passkeys** (`gwportal/passkeys.py`, `webauthn` 2.8): "Sign in with a
  passkey" instead of a password and code; each user adds and removes
  their own on the Account page (adding one needs the password too, so a
  stolen session can't add one). User verification (Face ID, fingerprint,
  the device's PIN) is required, so a passkey counts as both factors; an
  admin without two-factor codes (after `reset-2fa`) still sets them up.
  The relying party is `PORTAL_ORIGIN`'s host and the origin checked is
  `PORTAL_ORIGIN`, never the request's. Challenges are server-side, 5
  minutes, used once; the passkey gets a random user handle, not the
  email. Signing in with one goes through the same address limit,
  lockout and disabled checks as a password. `reset-2fa` removes them (a
  lost phone may hold a synced one). Up to 10 per account.
- **Microsoft 365** (`gwportal/microsoft.py`): "Sign in with Microsoft",
  and a tenant's Staff kept in step with a Microsoft 365 group (Staff →
  Microsoft 365). See [Microsoft 365 sign-in and sync](#microsoft-365-sign-in-and-sync).
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
  "Saved. It takes effect in a moment." (logged as a warning; gwphones
  retries it and alerts the operator, see "Saves not applied").
- **Call routing (admins only):** ring groups, IVR menus with their
  greetings (recorded in the page or from an audio file, converted to
  8 kHz there; up to 2 minutes, checked before reading), and business
  hours with "close now / open now"; and where the tenant's own numbers
  go (`PUT /api/pbx/inbound/{did}/target`: only that, never adding,
  taking or removing a number, which stays the operator's); and call
  links (address, QR code, where it rings, longest call, on/off, a new
  address). Video is a paid add-on, each tenant's own setting
  (`video`, the Tenants tab; off for a new tenant): with it, the tenant's
  extensions get video codecs and its admins can let a link allow the
  visitor's camera; without it, everything is audio only (a link's own
  video setting is kept but has no effect). The main tenant's is the
  "video calls" switch under Extensions, as before. Browser calls don't use a carrier, so they don't count
  in a tenant's calls at once (its plan, PSTN calls only); they're
  limited to protect the server and other tenants: 2 at once per link,
  the tenant's own `web_max` (default 10, set in the Tenants tab) and
  `web_max_total` over all tenants (default 30, Trunks & Outbound). Destinations come from gwphones as a labelled list
  (`/api/pbx/routing`), only ever the tenant's own. These routes sit on a
  router that refuses anyone but an admin.
- **Desk phones (admins only, `gwportal/deskphones.py`):** add a Yealink
  by its MAC on one of the tenant's extensions, move it, make a new
  provisioning password, view its settings (passwords hidden) and remove
  it, optionally giving its extension a new SIP password in the same step
  (the phone keeps its settings until it's factory-reset). The page shows
  the Server URL, user name and password to enter on the phone, the steps,
  and its web-page login. gwphones keeps it to the tenant's own phones,
  allows one desk phone per extension (the apps can share it: see
  "A desk phone and the apps on one extension"), and refuses
  a MAC another tenant has without saying so. The passwords are shown to
  the admin but never written to the audit log. Its own router, which
  refuses anyone but an admin.
- **Billing (admins only, `gwportal/billing.py`, gwphones'
  `tenant_billing.py` under `/api/pbx/billing/`):** the tenant's issued
  statements (cancelled ones hidden) with their PDFs (each download
  audited); this month so far, built the way a draft
  statement is (same rounding), as of the last billing pass, with calls
  not priced yet counted, not priced; and its calls out that connected,
  by month and extension (up to 2000), each at its statement price, else
  an estimate with its markup, else "—" (not priced yet). Prices only:
  both sides build every answer
  from an allowlist, so no cost, markup, margin, rate, trunk, route or
  rate sheet ever reaches it, nor anything of another tenant or the main
  tenant; a test looks for a distinctive cost and markup in every
  answer. A tenant's statements are its billing account's.
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
codes: it also removes their passkeys, forgets their trusted devices and
signs them out, and
they set two-factor codes up again at their next sign-in. Run it as the
portal's user: `cd /opt/gwphones/portal && sudo -u gwportal
venv/bin/python -m gwportal.cli reset-2fa --email ...`.

Settings (its own `.env`): `PORTAL_DB`, `PORTAL_ORIGIN` (e.g.
`https://portal.example.ie`), `PORTAL_KEY` (a Fernet key),
`PORTAL_GW_URL`, `PORTAL_GW_SECRET` (gwphones' `GWPHONES_PORTAL_SECRET`),
`PORTAL_BRAND`, and its own copy of `MS_GRAPH_*` and
`EMAIL_FROM_ADDRESS`.

### Microsoft 365 sign-in and sync

`gwportal/microsoft.py`, off until `PORTAL_MS_CLIENT_ID` and
`PORTAL_MS_CLIENT_SECRET` are in the portal's `.env`.

**What a tenant gets.** First their Microsoft 365 admin approves the app
once: Staff shows **Approve in Microsoft 365** (Microsoft's admin consent
page) before connecting, since the app is unverified and an ordinary user
can't consent to it. Then an admin opens Staff → **Connect Microsoft 365**
and signs in with their own work account: the Microsoft organisation in
that sign-in (its `tid`, from our own code exchange, never a value in a
redirect) is linked to the tenant, one organisation per tenant, audited.
The admin chooses:
- the **group** whose members use the phone system (name or object id),
  and optionally an **admins group** (members of both are admins);
- which **Exchange custom attribute** (1–15, `extensionAttributeN` in
  Graph) holds each person's extension. Set it in the Exchange admin
  centre: Recipients → Mailboxes → the mailbox → Custom attributes. With
  on-premises AD synced by Entra Connect, set the AD `extensionAttributeN`
  instead (it syncs up). Microsoft doesn't expose `ipPhone` to Graph.

Sync also fills in each person's **extension** (`plan_extensions`):
its name (their display name, accents dropped: phones and busy lamps
show it), voicemail by email to their own address (only if none is set),
and their **Office phone** as its caller ID, only if that's one of the
tenant's own numbers (the main one means the default). With **Create an
extension that doesn't exist yet** ticked (off by default), a number in
200–299 that isn't an extension yet is created, named after them. A
leaver's extension is switched off (nobody else on it), and switched on
again when they return; one an admin switched off stays off
(`ms_switched_off` keeps which ones sync switched off). Nothing is ever
deleted.

With **Put each person's extension on the site named by their Office**
(off by default), each extension goes on the site whose name matches
the person's Office in Microsoft 365 (`officeLocation`: admin centre,
the user, Contact information, Office; any case, spaces trimmed). An
Office that isn't one of the tenant's sites is reported and the site
left as it is, as it is with an empty Office; a number outside the
site's block is reported, not moved. New extensions are made on their
site.

**Preview** shows what a sync would do without doing it; **Sync now**
does it, and it also runs every 15 minutes. Staff need an extension that
exists in the tenant, one person each: not one another active person
already has (someone invited by email, say), which is reported instead;
an admin needs none. Someone still in the group with a problem like that
(or a number that isn't an extension: likely a typo) is left as they
are, not disabled. An **emptied** custom attribute means no phone: like
a leaver, their login is disabled and their extension switched off, both
back on when a number is put back. Someone moved to another number frees
the old one (switched off, if nobody else is on it). Staff itself also refuses a second
active person on an extension (inviting, moving, switching back on), and
flags older duplicates ("Shares ext"). People
synced in sign in with Microsoft (no password, no invitation); someone
who leaves the group, or whose Microsoft account is disabled, is
disabled and signed out, and enabled again if they come back. A local
person (invited by email) with the same address in the same tenant is
linked; the same address in another tenant is reported and skipped.
The last active admin is never disabled or demoted. An admin can still
**disable** a synced person in Staff (say, a compromised account), and
sync never enables them again (`disabled_by`: only people sync itself
disabled come back); their role and extension come from Microsoft 365
only. Guests (other organisations' people in the group) are skipped.
**Disconnect** stops it all and disables synced people without a
password; it doesn't withdraw the organisation's approval (their admin
removes the app under Enterprise applications for that).

**Signing in** finds the person only by Microsoft organisation and
object id (`tid`, `oid`), never by email (which a Microsoft admin sets
and nobody verifies). Microsoft replaces the password only: an admin
still gives the portal's two-factor code. The code flow uses PKCE, a
nonce and a state kept server-side for 10 minutes, used once, found by a
`SameSite=Lax` cookie (`__Host-gwportal-ms`: the session cookie is
Strict, so it isn't sent on Microsoft's redirect back). The id_token
comes straight from Microsoft's token endpoint, so its claims are
checked (issuer for its `tid`, audience, expiry, nonce) without fetching
signing keys (OpenID Connect Core 3.1.3.7). Only `organizations`
accounts: no personal Microsoft accounts.

**The app registration** (once, in our own Microsoft Entra admin centre,
a new app, not the one that sends email):
1. Entra admin centre → App registrations → New registration. Name
   "GW Phones portal"; **Accounts in any organizational directory
   (multitenant)**; redirect URI, platform **Web**:
   `https://portal.amapforyourmoney.com/api/microsoft/callback`.
2. Authentication → add a second Web redirect URI:
   `https://portal.amapforyourmoney.com/api/microsoft/consented`.
3. API permissions → Microsoft Graph: **Delegated** `openid`, `profile`,
   `email`; **Application** `User.Read.All` and `GroupMember.Read.All`.
   (Each customer's admin consents to these for their own organisation;
   grant consent in ours too, to test.)
4. Certificates & secrets → New client secret (it expires: at most 24
   months; put a reminder in the calendar). Copy the value straight into
   `/opt/gwphones/portal/.env` with an editor, never into chat:
   ```
   PORTAL_MS_CLIENT_ID=<Application (client) ID>
   PORTAL_MS_CLIENT_SECRET=<the secret's value>
   ```
   then `systemctl restart gwportal`.
5. The app is unverified (publisher verification needs a verified
   business), so users can't consent to it themselves: each customer's
   Microsoft 365 admin approves it first, which the Staff page asks for.

nginx: the `/api/microsoft/(signin/start|callback)` routes are in the
sign-in location's rate limit (`nginx-site.example`).

## Branches

- `main`: what runs. Desk phones (`desk-phones`) joined it on 15
  November 2026, after a T46G worked on it.

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

In the tenant portal (an extension's **Caller ID**), a tenant's admins set
it for any of its extensions and a user for their own, choosing only from
the tenant's own numbers: its main caller ID and the numbers routed to it
(`pbx.tenant_caller_ids`, stored as `+353...`). gwphones refuses any other
number from the portal, so a tenant can't show someone else's. The console
still sets any number; the portal shows one of those as "set by your
provider".

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

### Deleting a tenant

Console → Tenants: **Delete** shows on a **suspended** tenant, and asks
for its code to be typed again (`DELETE /api/pbx/tenants/{id}`, never the
main tenant). It's refused while the tenant still has numbers: remove or
move them first, or calls to them would fall to the main tenant's
default route.

It removes the tenant's extensions, ring groups, menus, hours, call
links, provisioning links, desk phones (and their Yealink RPS entries),
sites, settings and unapplied saves, and its phones' forwarding and do
not disturb (AstDB). Its **recordings, voicemail and greetings** are
moved aside (`<folder>/deleted/t<code>-<date>`, next to where they were)
and removed after **30 days** (`purge_deleted`, every 6 hours). Its row
stays with status `deleted`: its code is never used again, and billing
still knows whose its calls were; its **billing account is marked
closed** (`customers.closed_at`) and its statements and call records are
kept. The **portal** asks gwphones about each tenant it has people for,
hourly and at start (`gwportal/gone.py`); only on "Unknown tenant." does
it remove that tenant's logins, sessions, passkeys, devices and Microsoft
365 link. The change log says what was moved where.

The voicemail spool (`PBX_VM_SPOOL`, `/var/spool/asterisk/voicemail`)
may not let the service move a folder: then the change log says "left
at …", and it can be moved by hand as root
(`mv /var/spool/asterisk/voicemail/t<code> /var/spool/asterisk/voicemail/deleted/t<code>-<date>`).

### Extension numbers and sites

**3 or 4 digits, per tenant** (`ext_digits`, `pbx.EXT_PLANS`): a new
tenant gets **4 digits, 2000–8999** (the console's new-tenant form can
choose 3 for a small office); tenants from before, and the main tenant,
keep **3 digits, 200–299**. Every pattern follows the tenant's own
(`TenantNames.ext_pattern`/`ext_regex`): `_[2-8]XXX` for calling, `**`
pickup, click to call (`EXTEN:0:4`), forwarding targets, the feature
codes' own-extension check; recording names take 3 or 4 digits. Ring
groups (6XX), menus (5XX), hours (4XX) and park (700) stay 3 digits for
everyone: a phone sends the whole number, so 600 and 6000 can't be
confused. Logins stay tenant code + extension; the length tells the two
apart (1002201: 3 digits, 10032001: 4), so phones set up before keep
working. A tenant's digits are chosen when it's made and not changed
after (renumbering would need every phone set up again).

**Sites** (offices of one tenant; `sites`, `extensions.site_id`): one
tenant per business, however many offices, so they dial each other by
extension. Each site has:
- **its number**, one of the tenant's own: what its extensions show on
  calls out without their own caller ID (`effective_cid`: own, else the
  site's, else the tenant's), including **112/999**, so a call from Cork
  shows Cork's number and the emergency services get the right address
  (keep each number's address up to date with the carrier);
- optionally **a block of extension numbers** (Dublin 2000–2999, Cork
  3000–3999), inside the tenant's range and not overlapping another
  site's: a new extension of the site takes its next free number, and an
  extension outside the block can't be put on it;
- **its own pickup group**: `*8` answers a ringing phone of the same site
  only (`named_call_group`/`named_pickup_group` `<prefix>-site<id>`);
  `**<ext>` still answers a given extension anywhere.

Managed in the portal (admins: Extensions → Sites, a Site on each
extension, extensions grouped by site) and the console (Extensions →
Sites). A site with extensions can't be removed, and its block can't
shrink past them.

### A desk phone and the apps on one extension

An extension takes up to 4 devices at once (`max_contacts=4` on each
generated AOR, over the `aor-ext` template's one; `remove_existing` still
drops the oldest beyond that), all with the extension's own password: a
desk phone, Groundwire through its push server, another app. A call for
the extension, from a ring group, or click to call rings every connected
device through `[sub-ring-ext]` (`Dial(PJSIP/<login>)` alone rings only
the first contact; it uses `PJSIP_DIAL_CONTACTS`). The first answered
takes the call; Dial tells the others it was answered elsewhere, so
they don't show a missed call. Not Dial's `c` option: on Asterisk 20 it
sets "answered elsewhere" on every cancel ("If the Dial() application
cancels this call, always set HANGUPCAUSE to 'answered elsewhere'"), so a
caller hanging up or a ring-out to voicemail wouldn't show as missed. Voicemail, its lamp, do not
disturb, forwarding, busy lamps, caller ID, recording and billing are the
extension's, so they're the same on every device.

**When a call comes in** (portal: the extension; console: Phone setup),
per extension (`ring_mode`, `RINGMODE_<login>` in `[globals]`):
- **all** (default): every device at once;
- **desk_first**: the desk phone alone for 10 s, then every device (and
  straight to every device if the desk phone is busy);
- **backup**: the desk phone only, or the apps when no desk phone is
  connected.
With only one kind connected, every mode rings what's there. A desk phone
is told from an app by the User-Agent it registered with (`DESK_UA_RE`:
Yealink, Cisco, Panasonic, ...). Ring groups ring every device of each
member. Admins set it for any extension, a user for their own.

**Each device** is listed (portal: the extension's "Phones on this
extension" and Status; console: Phones), as what it is and whether it's
online: `extension_devices` matches `pjsip show contacts` (status, round
trip) with `database show registrar/contact` (each contact's User-Agent
and address, by its id `aor;@hash`), and `device_label` names it ("Yealink
T46G desk phone", "Groundwire app (through its push server)", "Linphone
app", else the User-Agent). The portal gets the name, desk or app, and
online only; the console also the address, round trip and User-Agent.
For a desk phone, admins (portal) and the console also get its **MAC**
(from Desk phones) and its **office address** (`lan_ip`: the
`x-ast-orig-host` Asterisk keeps when it rewrites a contact behind NAT,
only a private address), linked to its web page, which opens only from a
computer on that office network.

**Call pull, `*11`:** on a call on one of the extension's devices, dial
`*11` on another: the call moves there and the first device drops out.
`[t…-features]` looks only at channels of the dialling extension's own
login (from its endpoint's `PBX_EXT`, never the caller ID), skips its own
channel, takes the first one bridged to someone (`BRIDGEPEER`) and
`Bridge()`s that party in (option `x`: hung up when this call ends, not
sent back to the dialplan). Nothing to take over: "invalid". A call
being recorded stops recording at the move (the recording is on the
first device's channel).

Not ExecIf around a device list: it splits on `:`, and every contact's
address has one (`sip:`), so the dialplan uses GotoIf and Set instead.

### Saves not applied

A save (from the console or the portal) that's in the database but
couldn't be applied to Asterisk, for example while Asterisk restarts, is
kept in `apply_pending` with when, what, its tenant and who made it. Any
apply that works clears them all, since every apply writes everything.
gwphones retries by itself, at once and then after 1, 2, 4, 8 and every
15 minutes; retries add no rows. If changes are still not live after
`PBX_ALERT_AFTER_MINUTES` (default 5), the operator is emailed once
through Microsoft Graph, and again when they're applied. In `.env`:

```
PBX_ALERT_EMAIL=you@example.ie
```

Without it the alert is only logged in `pbx-changes.log`. The console's
Status shows them (when, what, tenant, who, the last error, the next try)
with **Apply now**; the portal never sees them. The retry and alert state
is in memory, so a restart may send one more alert.

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

### Voicemail PINs

A PIN can be chosen in the portal (Phones, Voicemail access), and each
extension can let `*97` from its own phones skip the PIN (`vm_nopin`:
`set_var=VM_NOPIN=1` on its endpoint; `*98` and every other way in still
asks). A PIN can also be changed from the phone's voicemail menu: Asterisk
would write that into the generated mailbox file, which the next save
overwrites, so it goes through `asterisk/vm-pin.sh` instead. Asterisk runs
it to check a new PIN (the portal's rules: 4-8 digits, no repeats, runs
or the extension number) and to save it, which drops it in
`/var/spool/asterisk/vm-pins` (`PBX_VM_PINS`); the service saves it within
a few seconds like a portal PIN and rewrites the file. Set up once:

```
install -m 755 asterisk/vm-pin.sh /usr/local/bin/asterisk-vm-pin
install -d -o asterisk -g asterisk -m 2770 /var/spool/asterisk/vm-pins
```

in the `[general]` section of `/etc/asterisk/voicemail.conf`:

```
externpass=/usr/local/bin/asterisk-vm-pin set
externpasscheck=/usr/local/bin/asterisk-vm-pin check
```

and `-/var/spool/asterisk/vm-pins` in the unit's `ReadWritePaths` (as in
the example unit). PINs are never written to the logs.

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

**A tenant is a customer.** Each tenant but the main one (the
operator's own, never billed) has exactly one billing account, made with
the tenant (Billing, Customers (tenants): name on its statements, email,
markup, notes); there's no adding or deleting one by hand. It pays your
cost plus a markup (the default, or its own) for every priced call of
the tenant's, for the **phone numbers** (DDIs) given to it, which have a
monthly cost ex VAT (or a fixed monthly price) and the months they're
active, and for its add-ons. **What each customer owes** shows, per
month, calls, minutes, cost, price and margin, flags a tenant with calls
not priced yet (left out of the totals until they are), and downloads as
CSV, overall or itemised. The main tenant's calls show as "Not billed
(main tenant)", for information. Until 17 November 2026 customers were
groups of extensions, made before tenants; that one-off change removed
the main tenant's (test data) with their numbers, and kept each other
tenant's first. Issued statements keep their own copy, so they still
open.

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

**Add-ons** (so far video): a tenant with video on is charged its
`video_price` (default €5.00 a month, set per tenant in the Tenants tab)
for every month it was on at any point. Each billing pass, and each change
to a tenant, marks the current month in `addon_months` at the price then;
a month on a statement keeps its price, and cancelling the statement
releases it. It goes on the tenant's own statement. The main tenant (the
operator's own) is never charged. Statements show an
Add-ons line and section; ones issued before add-ons still render.

Rate sheets are large, so the Tailscale nginx site needs a bigger upload
limit in its `/phone-api/` location (110m since desk-phone firmware, see
Desk phones; `/admin-api/` is the VPS dashboard's since the split):

```nginx
        client_max_body_size 110m;
```

### Caller IDs and carriers

Carriers block calls showing their own country's numbers that arrive from
abroad (ComReg's blocking of Irish numbers on calls from abroad;
Australia's code C661), so a caller ID has to leave through a carrier in
its own country. Each route can list the calling codes of the caller IDs
it can show (Trunks & Outbound, `cli_<route>`, e.g. `353` or `61,353`;
empty means any, as before). The route check (`lcr.py`) then only offers
routes that can show the call's caller ID; if none can, it answers
`OK:<routes>|CLI:<main number>` and the dialplan shows the tenant's main
number instead (logged in `pbx-changes.log`); if not even that, the call is
refused. 112/999 skip the route check and always use Irish VoIP, so an
extension whose own caller ID Irish VoIP can't show (`EMERG_OWN_CID=0` on
its endpoint) shows the main number on emergency calls. Nothing of this is
generated while no route has a list. If the route check doesn't answer
(the service restarting), calls go out in trunk order without the rule,
as they always did then. Caller IDs outside Ireland must be written in
full (+61...): one starting with 0 is taken as Irish.

### WhatsApp calls (`backend/whatsapp.py`)

Customers tap the call button in the business's WhatsApp chat, and Meta's
SIP gateway hands the call to our Asterisk. From there it's routed like
any trunk call, by the WhatsApp number's inbound route (ring group, menu,
hours, voicemail, recording). Phones show "WhatsApp" as the caller's name.
Calls out to WhatsApp users (which need the user's permission) aren't
set up. For now this is for the main tenant's number only.

How it fits together:
- **Meta → us:** TLS to `pbx.amapforyourmoney.com` (the RSA certificate)
  on our TLS port 15061, SDES-SRTP, Opus or G.711 a-law. Meta's calls
  are matched to the `[whatsapp]` endpoint by Meta's source addresses only;
  the `X-FB-External-Domain` header in Meta's own example is never
  trusted (anyone can send it). Inbound calls aren't challenged (Meta
  answers a 407, Asterisk asks with a 401), like the other trunks.
- **The endpoint** lives in `/etc/asterisk/pjsip_whatsapp.conf`, written
  by `whatsapp.py write-conf` (root:asterisk, 0640: it holds Meta's SIP
  password, used when Meta challenges our requests in a call). Its context
  `[from-whatsapp]` (static `asterisk/extensions.conf`) only goes to
  `[from-trunk]`, so it reaches inbound routes and never a trunk.
- `rewrite_contact=no` is required: with it on, our 200 OK loses Meta's
  Record-Route, there's no ACK and the audio stops after about 30 s.
- **Billing:** `whatsapp` is an inbound-only trunk (`billing.INBOUND_TRUNKS`),
  shown as "WhatsApp" in call history, never a choice for calls out.
  Incoming calls are free here; check Meta's current pricing for user-started calls.
- With SIP on, Meta's own calling API and webhooks stop for that number
  (Meta's choice): everything happens in Asterisk.

**On Meta's side first** (business.facebook.com, developers.facebook.com):
1. A Meta Business account, with **business verification**. Calling needs
   the number's messaging limit at 2,000 or more.
2. An app (Business type) with the WhatsApp product, in **Live** mode
   (that needs a privacy policy URL).
3. The number: one of the main tenant's Irish numbers, verified by a
   **voice call** (route it to a phone or voicemail first). A number in
   the ordinary WhatsApp or WhatsApp Business app normally has to leave it
   first (Meta has a "coexistence" option for keeping both, not checked).
4. A **system user** with `whatsapp_business_management` and
   `whatsapp_business_messaging` on the app and WhatsApp account, and a
   permanent token for it. Keep the token in a password manager: the
   tool asks for it each time (never on the command line, never saved).
5. The number's **phone number id** (WhatsApp Manager or the app's API
   setup page) and the app's **Graph API version** (App settings →
   Advanced), e.g. `v24.0`.

**On the server** (as root, from `/opt/gwphones/backend`):
```
asterisk -rx "core show version"
asterisk -rx "core show translation" | grep -i opus     # an Opus translator?
grep -A15 '^\[transport-tls\]' /etc/asterisk/pjsip.conf  # no require_client_cert (Meta's client cert is from its own CA)
grep -A6 '^\[global\]' /etc/asterisk/pjsip.conf          # endpoint_identifier_order, if set, must include ip
W="venv/bin/python whatsapp.py --phone-id <id> --graph-version <vNN.0>"
$W check
$W prepare                       # calling on, SDES keys, a-law as well as Opus (before SIP)
$W enable-sip --host pbx.amapforyourmoney.com --port 15061
$W write-conf                    # add --codecs alaw if there's no Opus translator
```
Then once, in `/etc/asterisk/pjsip.conf` (at the end): `#include pjsip_whatsapp.conf`,
and `asterisk -rx "module reload res_pjsip.so"`. Check with
`asterisk -rx "pjsip show endpoint whatsapp"`. Copy the new
`[from-whatsapp]` context from `asterisk/extensions.conf` into
`/etc/asterisk/extensions.conf` and `asterisk -rx "dialplan reload"`. The
firewall must let Meta's addresses reach 15061/tcp and the RTP ports.
`$W disable-sip` stops the calls coming to us; `write-conf` again after
Meta's SIP password is reset. (`enable-sip` sends the port as a number,
as in Meta's example; Meta's table calls it a string: if Meta refuses it,
that's a one-line change.) Meta's address list (from its integration
examples page) can be replaced with `--ranges-file`.

### PBX certificate

**It must be an RSA certificate, not ECDSA.** Desk phones' firmware (a
Panasonic KX-HDV130, older Yealinks) often offers only RSA cipher suites,
and with an ECDSA certificate the TLS handshake fails ("no shared cipher"
in Asterisk's log); the apps don't mind either way. Since 11 November
2026 the `pbx.amapforyourmoney.com` lineage is RSA 2048 (`--key-type rsa`,
kept by certbot at every renewal).

**The TLS transport needs a cipher list without DHE.** Asterisk
(pjproject) has no DH parameters, so when a phone offers DHE-RSA ciphers
first, as a T46G does, Asterisk picks one and the handshake fails:
`SSL_ERROR_SSL (Handshake) ... internal error` in
`/var/log/asterisk/messages.log`, alert 80 at the phone, and the phone
shows "No service". In `[transport-tls]` in `/etc/asterisk/pjsip.conf`,
under `method=tlsv1_2` (since 15 November 2026):

```
cipher=ECDHE-RSA-AES256-GCM-SHA384,ECDHE-RSA-AES128-GCM-SHA256,AES256-GCM-SHA384,AES128-GCM-SHA256,AES256-SHA,AES128-SHA
```

The apps keep ECDHE; a T46G (no ECDHE) gets `AES256-GCM-SHA384`, without
forward secrecy, and calls are still encrypted (SRTP). A transport change
needs `asterisk -rx "core restart when convenient"`, not a reload. To
check, these should give `Cipher is AES256-GCM-SHA384` with no alert, and
`Verify return code: 0 (ok)`:

```
openssl s_client -connect pbx.amapforyourmoney.com:15061 -servername pbx.amapforyourmoney.com \
  -tls1_2 -cipher 'DHE-RSA-AES256-GCM-SHA384:AES256-GCM-SHA384' </dev/null 2>&1 | grep -E 'Cipher is|Verify return|alert'
```

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

### Desk phones (Yealink, `backend/deskphones.py`)

A phone fetches `<mac>.cfg` from `/prov/yealink/` with its own username
(its MAC) and password, shown in the console under Desk phones, RPS
details. A tenant with its own SIP hostname is given
`https://<that hostname>/prov/yealink/` (e.g.
`https://test.sip.amapforyourmoney.com/prov/yealink/`), the name its
phones register to, so its phones follow it if it's moved by DNS; a
phone's file is never served under another tenant's hostname. Other
tenants get `PBX_DESKPHONE_BASE_URL`, or `PBX_PROVISION_BASE_URL` when
that isn't set:

```
PBX_DESKPHONE_BASE_URL=https://pbx.amapforyourmoney.com
```

**That site must have an RSA certificate.** A T46G on its last firmware
(28.83.0.120) speaks TLS 1.2 but offers only RSA and DHE-RSA ciphers, no
ECDHE. Against `amapforyourmoney.com`'s ECDSA certificate the handshake
fails straight away (alert 40, `handshake_failure`) and nothing shows in
the access log; `tcpdump -X` on port 443 shows it. The nginx site
`pbx-prov` serves `pbx.amapforyourmoney.com` and the tenants' hostnames
(`server_name pbx.amapforyourmoney.com *.sip.amapforyourmoney.com;`) with
the RSA certificate from the section above, which covers both, and passes `/prov/yealink/` to port 8010; keep its
ciphers including `DHE-RSA-AES256-GCM-SHA384`. To check what the phone
needs:

```
openssl s_client -connect pbx.amapforyourmoney.com:443 -servername pbx.amapforyourmoney.com \
  -tls1_2 -cipher DHE-RSA-AES256-GCM-SHA384 </dev/null 2>&1 | grep -E 'Cipher is|alert'
```

Firmware older than 28.83 (e.g. 28.80) fails before that. The T46G's
last official firmware is Yealink's
`https://www.yealink.com/upfiles/products/201909/1568789449754.83.0.120.rom`
(phone web page, Settings, Upgrade). The T46G is end of life, so RPS
doesn't take it: enter the URL, username and password on the phone's web
page (Settings, Auto Provision, then Autoprovision Now), and again after
every factory reset, which clears them. A factory-reset phone may need
Security, Trusted Certificates, "Only Accept Trusted Certificates" turned
off for its first fetch; the config turns it back on
(`static.security.trust_certificates = 1`). The T46G on 28.83.0.120
trusts Let's Encrypt's root, so later fetches work with it on. The
config also turns on Common Name Validation
(`static.security.cn_validation = 1`), so the certificate must be for the
server the phone reaches, not just any trusted one; checked on the T46G
against `pbx.amapforyourmoney.com` and against a tenant's own SIP hostname
(`test.sip.amapforyourmoney.com`, the `*.sip` wildcard), set up from the
portal on 15 November 2026.

Once it has fetched its config the phone's web page login is `admin` with
the admin password shown under RPS details (the config sets it), not
`admin`/`admin`. Watch a fetch with
`tail -f /var/log/nginx/pbx-prov.access.log /var/log/admin-dashboard/pbx-changes.log`:
a 401 and "refused … wrong credentials" means the username or password is
wrong; 10 wrong tries from one IP give a 429 for 10 minutes.

**Front page** (`site/index.html`): https://pbx.amapforyourmoney.com/
shows a static GW Phones page (what the platform does, a link to the
customer portal, info@amapforyourmoney.com), public proof of the service,
e.g. for Yealink's RPS application. Self-contained: no scripts, fonts or
images from elsewhere. The `pbx-prov` site serves it at `/` only, before
its `location /` that returns 404:

```nginx
    location = / {
        root /opt/gwphones/site;
        try_files /index.html =404;
        add_header Content-Security-Policy "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy no-referrer always;
    }
    # The privacy policy (site/privacy.html), e.g. for the Meta app (WhatsApp calls).
    location = /privacy {
        root /opt/gwphones/site;
        try_files /privacy.html =404;
        default_type text/html;
        add_header Content-Security-Policy "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" always;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy no-referrer always;
    }
```

A `git pull` updates them; no build. The privacy policy is
https://pbx.amapforyourmoney.com/privacy. The tenants' `*.sip` names share the
site, so they show it too.

**Yealink RPS** (`backend/rps.py`): with an RPS Enterprise account's
access key in `.env`,

```
PBX_RPS_ACCESS_KEY=...
PBX_RPS_ACCESS_SECRET=...
# PBX_RPS_API_URL=https://api-dm.yealink.com:8443  (the default)
```

adding a phone (console or portal) also registers its MAC in Yealink's
redirection service with its own provisioning URL (the tenant's, as
above), username (the MAC) and password, so a new or factory-reset phone
that supports RPS sets itself up when plugged in: no server entries, no
typing on the phone. A new provisioning password updates it there;
removing the phone removes it there. A failure (Yealink unreachable, or
the MAC "registered to another company", which needs that company or a
Yealink ticket to release it) never stops the save: the phone shows it
(the console the reason, the portal a plain message) with **Sync with
RPS** to try again, and can always be set up by hand. The signing
follows Yealink's "Json API for RPS Management Platform" V3.6.0.20
(HMAC-SHA256 over the method, the X-Ca-* headers, Content-MD5 and the
path; tested against its worked examples). The provisioning password
goes to Yealink, which is how RPS hands it to the phone; nothing secret
is logged. Older, end-of-life models (T41P, T46G) may not be served by
RPS (a third-party guide says so; Yealink's API document doesn't say):
**Test RPS connection** in the console (read-only: Yealink's answer for a
MAC, and our server entries) settles it and tests the keys and signing
before any phone is registered. Each call waits at most 4 s, so a phone's
sync (up to three calls) stays inside the portal's 15 s.

**Firmware library** (console, Desk phones, Firmware; server-wide, never
the portal). Upload a model's `.rom` with its version (as the phone's
Status page shows it); files go to `/var/lib/admin-dashboard/firmware/`
(`PBX_FIRMWARE_DIR`), up to 100 MB, checked not to be a zip or a web
page. Choose a version as the model's target, and a phone of that model
that reports anything else in its user agent (`Yealink SIP-T46G
28.83.0.120 <mac>`) gets `static.firmware.url` in its config, with its own
credentials in the address, and installs it at its next check (it
restarts). A phone already on the target, of another model, or not saying
what it runs gets no such line, so nothing is downloaded every hour. A
phone still on another version after 3 configs with the line (usually the
file isn't the version it was labelled) is left alone, logged once and
shown as stuck in the console; choosing the target again tries again.
Older versions stay in the library to go back to, but never below what a
phone needs to reach this server: a T46G below 28.83 can't connect, so it
would strand itself. The target can't be removed. The files are served under `/prov/yealink/firmware/` only to a
phone with valid credentials. `pbx-changes.log` notes each "told to
upgrade", each download, and each "firmware A -> B". A phone has to reach
the server first: a T46G below 28.83 can't, so its first upgrade is by
hand. Uploading needs the Tailscale site's `/phone-api/` location (the
console's; `/admin-api/` is the VPS dashboard's) to take the size; without
it nginx's 1 MB default refuses it ("client intended to send too large
body" in `/var/log/nginx/error.log`):

```nginx
        client_max_body_size 110m;
```

A desk phone can share its extension with the apps (see "A desk phone
and the apps on one extension"). A phone can be moved
to another extension in the console without a new password; it picks it
up at its next fetch. If it then shows "No service", see the cipher list
in the PBX certificate section.
