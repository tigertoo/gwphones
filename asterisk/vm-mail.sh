#!/bin/sh
# /usr/local/bin/asterisk-vm-mail
# Asterisk's mailcmd (voicemail.conf [general]): Asterisk pipes each "new
# voicemail" email in here. There's no mail server on this VPS, so this
# only drops it in the outbox; the admin dashboard sends it on through
# Microsoft Graph and deletes it. Needs no passwords or mail settings.
OUTBOX=${VM_OUTBOX:-/var/spool/asterisk/vm-outbox}
umask 007
tmp=$(mktemp "$OUTBOX/.in.XXXXXX") || exit 1
chmod 660 "$tmp"
cat > "$tmp" && mv "$tmp" "$OUTBOX/$(date +%s).$$.eml"
