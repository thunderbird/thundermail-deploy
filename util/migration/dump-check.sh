#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need OLD_STALWART_URL OLD_STALWART_USER OLD_STALWART_PASSWORD
PRINCIPALS=${1:-${SECRET_DIR:+$SECRET_DIR/principals.json}}
[ -s "${PRINCIPALS:-}" ] || die "usage: dump-check.sh [principals.json] (default \$SECRET_DIR/principals.json)"
jq -e 'type == "array"' "$PRINCIPALS" >/dev/null || die "$PRINCIPALS is not a principals dump"
TYPES="individual group resource location list other domain tenant role apiKey oauthClient"
cred() {
  local v="$OLD_STALWART_USER:$OLD_STALWART_PASSWORD"
  case $v in *[$'\n\r']*) return 1 ;; esac
  v=${v//\\/\\\\}
  printf 'user = "%s"\n' "${v//\"/\\\"}"
}
legacy_loopback >/dev/null || die "$(legacy_loopback)"
bad=0
printf '%-12s %8s %8s\n' TYPE SERVER DUMP
for t in $TYPES; do
  server=$(curl -sk -m 30 -K <(cred) "${OLD_STALWART_URL%/}/api/principal?types=$t&limit=1&page=1" | jq -er '.data.total | numbers' 2>/dev/null) || server=error
  dump=$(jq --arg t "$t" '[.[] | select(.type == $t)] | length' "$PRINCIPALS")
  printf '%-12s %8s %8s\n' "$t" "$server" "$dump"
  [ "$server" = "$dump" ] || bad=$((bad + 1))
done
[ "$bad" = 0 ] || die "dump-check: $bad type(s) differ between the server and $PRINCIPALS; do not convert this dump"
echo "dump-check: every principal type matches ($(jq length "$PRINCIPALS") principals)"
