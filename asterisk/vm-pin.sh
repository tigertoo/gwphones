#!/bin/sh
# /usr/local/bin/asterisk-vm-pin
# Voicemail PINs changed from a phone's voicemail menu (voicemail.conf
# [general]):
#   externpasscheck=/usr/local/bin/asterisk-vm-pin check
#       Asterisk adds: mailbox context oldpin newpin. Prints VALID or
#       INVALID, by the portal's rules (4-8 digits, no repeats like 1111,
#       no runs like 1234 or 9876, not the extension number).
#   externpass=/usr/local/bin/asterisk-vm-pin set
#       Asterisk adds: context mailbox newpin. Asterisk then doesn't write
#       the PIN into the generated mailbox file (gwphones would overwrite
#       it at the next save): this drops it in the spool for gwphones,
#       which saves it like a PIN chosen in the portal and rewrites the
#       file. Needs no passwords: like asterisk-vm-mail.
SPOOL=${VM_PINS:-/var/spool/asterisk/vm-pins}

ok_pin() {  # $1 pin, $2 mailbox
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ ${#1} -ge 4 ] && [ ${#1} -le 8 ] || return 1
    [ -z "$(printf %s "$1" | tr -d "$(printf %.1s "$1")")" ] && return 1   # one digit repeated
    case 01234567890 in *"$1"*) return 1 ;; esac                            # a run up
    case 09876543210 in *"$1"*) return 1 ;; esac                            # a run down
    case "$1" in *"$2"*) return 1 ;; esac                                    # the extension
    return 0
}

case "$1" in
    check)
        if ok_pin "$5" "$2"; then echo VALID; else echo INVALID; fi ;;
    set)
        context=$2 mailbox=$3 pin=$4
        case "$context$mailbox" in *[!A-Za-z0-9_-]*|'') exit 1 ;; esac
        ok_pin "$pin" "$mailbox" || exit 1
        umask 007
        tmp=$(mktemp "$SPOOL/.in.XXXXXX") || exit 1
        printf '%s %s %s\n' "$context" "$mailbox" "$pin" > "$tmp" && mv "$tmp" "$SPOOL/$(date +%s).$$.pin" ;;
    *)
        exit 1 ;;
esac
