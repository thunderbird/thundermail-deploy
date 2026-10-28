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
total() { curl -sk -m 30 -K <(cred) "${OLD_STALWART_URL%/}/api/principal?${1:+types=$1&}limit=1&page=1" | jq -er '.data.total | numbers' 2>/dev/null; }
all=$(total) || die "dump-check: the legacy principal total is unreadable"
bad=0
known=0
printf '%-12s %8s %8s\n' TYPE SERVER DUMP
for t in $TYPES; do
  server=$(total "$t") || server=error
  dump=$(jq --arg t "$t" '[.[] | select(.type == $t)] | length' "$PRINCIPALS")
  note=""
  if [ "$server" = "$dump" ]; then
    known=$((known + server))
  elif [ "$t" = tenant ] && [ "$server" = error ] && [ "$dump" = 0 ]; then
    note="unreadable on both sides"
  elif [ "$server" = "$all" ]; then
    note="filter ignored, overall total"
  else
    bad=$((bad + 1))
  fi
  printf '%-12s %8s %8s%s\n' "$t" "$server" "$dump" "${note:+ $note}"
done
[ "$known" = "$all" ] || { echo "matched per-type counts sum to $known, the server total is $all"; bad=$((bad + 1)); }
[ "$(jq length "$PRINCIPALS")" = "$all" ] || { echo "the dump holds $(jq length "$PRINCIPALS") principals, the server total is $all"; bad=$((bad + 1)); }
[ "$bad" = 0 ] || die "dump-check: $bad check(s) failed between the server and $PRINCIPALS; do not convert this dump"
echo "dump-check: every principal type matches ($(jq length "$PRINCIPALS") principals)"
