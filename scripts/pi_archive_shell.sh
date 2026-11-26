#!/bin/bash
# pi_archive_shell.sh -- the only thing the "pi-archive" SSH key may run on the Pi.
#
# Installed as /usr/local/sbin/pi-archive-shell and bound to the key in
# ~pi/.ssh/authorized_keys as
#     restrict,command="/usr/local/sbin/pi-archive-shell" ssh-ed25519 AAAA... pi-archive
# so whatever the PC asks for arrives in SSH_ORIGINAL_COMMAND and nothing else runs.
# The PC side is scripts/pi_log_archive.py (a compose service), which moves closed
# log segments (lux.jsonl.<stamp>, temp.jsonl.<stamp>) to the computer after 14 days.
#
#   list              one line per closed segment:  NAME SIZE MTIME_EPOCH
#   sum NAME          sha256 of the segment
#   cat NAME          its bytes
#   rm NAME SHA256    delete it, but only if it was closed more than a day ago
#                     and SHA256 is the checksum of its bytes right now (so the
#                     caller must hold the exact data)
#
# NAME must be a closed segment in /home/pi. The live files (lux.jsonl,
# temp.jsonl), links and anything else are refused.
set -u
DIR=/home/pi
RE='^(lux|temp)\.jsonl\.([0-9]+|[0-9]{8}T[0-9]{6}Z(-[0-9]+)?)$'
MIN_AGE_S=86400

read -r verb name sha extra <<< "${SSH_ORIGINAL_COMMAND:-}"
[ -z "${extra:-}" ] || { echo "too many arguments" >&2; exit 2; }

ok_name() {
    [[ "${1:-}" =~ $RE ]] && [ -f "$DIR/$1" ] && [ ! -L "$DIR/$1" ]
}

case "$verb" in
    list)
        for f in "$DIR"/lux.jsonl.* "$DIR"/temp.jsonl.*; do
            b=$(basename "$f")
            ok_name "$b" || continue
            printf '%s %s %s\n' "$b" "$(stat -c %s "$f")" "$(stat -c %Y "$f")"
        done
        ;;
    sum)
        ok_name "$name" || { echo "not a closed segment" >&2; exit 2; }
        sha256sum "$DIR/$name" | cut -d' ' -f1
        ;;
    cat)
        ok_name "$name" || { echo "not a closed segment" >&2; exit 2; }
        cat "$DIR/$name"
        ;;
    rm)
        ok_name "$name" || { echo "not a closed segment" >&2; exit 2; }
        [[ "${sha:-}" =~ ^[0-9a-f]{64}$ ]] || { echo "need the sha256" >&2; exit 2; }
        age=$(( $(date +%s) - $(stat -c %Y "$DIR/$name") ))
        [ "$age" -gt "$MIN_AGE_S" ] || { echo "closed less than a day ago" >&2; exit 3; }
        now=$(sha256sum "$DIR/$name" | cut -d' ' -f1)
        [ "$now" = "$sha" ] || { echo "checksum differs" >&2; exit 4; }
        rm -- "$DIR/$name" && echo removed
        ;;
    *)
        echo "unknown command" >&2
        exit 2
        ;;
esac
