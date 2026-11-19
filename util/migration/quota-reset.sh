#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD STALWART_ADMIN_ACCOUNT_ID
TIMEOUT=${QUOTA_RESET_TIMEOUT:-600}
MODE=wait
case "${1:-}" in
  '') ;;
  --no-wait) MODE=no-wait ;;
  status) MODE=status ;;
  *) die "usage: quota-reset.sh [--dry-run | --apply [--no-wait]] | status [<task id>]" ;;
esac
API=${STALWART_URL%/}/jmap
USING='["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]'
cred() {
  local v="$STALWART_USER:$STALWART_PASSWORD"
  case $v in *[$'\n\r']*) return 1 ;; esac
  v=${v//\\/\\\\}
  printf 'user = "%s"\n' "${v//\"/\\\"}"
}
jmap() { curl -sS -m 60 -K <(cred) -H 'Content-Type: application/json' --data "$1" "$API"; }
call() { jmap "$(jq -cn --arg a "$STALWART_ADMIN_ACCOUNT_ID" --arg m "$1" --argjson args "$2" --argjson using "$USING" '{using: $using, methodCalls: [[$m, ({accountId: $a} + $args), "0"]]}')" | jq -ce '.methodResponses[0] | if .[0] == "error" then error(.[1] | tostring) else .[1] end'; }
queued() { call x:Task/query "$(jq -cn --arg t "$1" '{filter: {"@type": $t}, limit: 5000, calculateTotal: true}')" | jq -re 'if (.ids | type) == "array" then (.total // (.ids | length)) else error("no ids") end'; }
usage() {
  local ids
  local ids='[]' page list='[]' i got
  while :; do
    page=$(call x:Account/query "$(jq -cn --argjson p "$(jq 'length' <<<"$ids")" '{position: $p, limit: 1000}')" | jq -ce '.ids') || return 1
    [ "$(jq 'length' <<<"$page")" -gt 0 ] || break
    ids=$(jq -c --argjson p "$page" '. + $p' <<<"$ids")
  done
  for ((i = 0; i < $(jq 'length' <<<"$ids"); i += 200)); do
    got=$(call x:Account/get "$(jq -c --argjson i "$i" '{ids: .[$i:$i + 200], properties: ["usedDiskQuota"]}' <<<"$ids")" | jq -ce '.list') || return 1
    list=$(jq -c --argjson l "$got" '. + $l' <<<"$list")
  done
  jq -r '"accounts=\(length) with_usage=\([.[] | select((.usedDiskQuota // 0) > 0)] | length) used_bytes=\([.[].usedDiskQuota // 0] | add // 0)"' <<<"$list"
}
if [ "$MODE" != wait ]; then
  . "$MIG_DIR/timing.sh"
fi
if [ "$MODE" = status ]; then
  id=${2:-$(awk -F'\t' '$1 == "cut-ecs" {v = ""} $1 == "quota-reset-started" {v = $5} END {print v}' "$TIMING_TSV" 2>/dev/null || true)}
  [ -n "$id" ] || { echo "quota reset status: no quota-reset-started row after the last cut-ecs row and no task id given"; exit 1; }
  s=$(queued StoreMaintenance) && a=$(queued AccountMaintenance) || { echo "quota reset status: x:Task/query failed"; exit 1; }
  printf 'quota reset task %s: queued StoreMaintenance=%s AccountMaintenance=%s\n' "${id:-unknown}" "$s" "$a"
  if [ "$s" = 0 ] && [ "$a" = 0 ]; then
    u=$(usage) && [ -n "$u" ] || { echo "quota reset status: the queues are empty but the account usage read failed: usage not read"; exit 1; }
    echo "DONE  quota reset finished: $u"
    exit 0
  fi
  echo "WAIT  quota reset task ${id:-unknown} not finished: $a account task(s) and $s store task(s) still queued"
  exit 2
fi
now=$(date -u +%FT%TZ)
body=$(sed "s/__ACCOUNT_ID__/$STALWART_ADMIN_ACCOUNT_ID/; s/__NOW__/$now/g" "$MIG_DIR/quota-reset.json.tpl")
jq -e '.methodCalls[0][1].create["quota-reset"].maintenanceType == "resetUserQuotas"' <<<"$body" >/dev/null || die "quota-reset.json.tpl did not render"
if [ "$APPLY" != 1 ]; then
  printf 'dry-run: POST %s x:Task/set StoreMaintenance resetUserQuotas, accountId %s, due %s\n' "$API" "$STALWART_ADMIN_ACCOUNT_ID" "$now"
  if [ "$MODE" = no-wait ]; then
    printf 'dry-run: then print the task id, write a quota-reset-started row and return without waiting; quota-reset.sh status reads whether it is done\n'
  else
    printf 'dry-run: then poll x:Task/query StoreMaintenance and AccountMaintenance until both are 0 (timeout %ss), then x:Account/get usedDiskQuota\n' "$TIMEOUT"
  fi
  exit 0
fi
r=$(jmap "$body" | jq -ce '.methodResponses[0] | if .[0] == "error" then error(.[1] | tostring) else .[1] end') || die "x:Task/set failed"
jq -e '.created["quota-reset"].id' <<<"$r" >/dev/null || die "quota reset task not created: $(jq -c '.notCreated' <<<"$r")"
echo "created task $(jq -r '.created["quota-reset"].id' <<<"$r") due $now"
if [ "$MODE" = no-wait ]; then
  printf 'quota-reset-started\t%s\t%s\ttask\t%s\n' "$now" "$(date -u +%FT%TZ)" "$(jq -r '.created["quota-reset"].id' <<<"$r")" >> "$TIMING_TSV"
  echo "not waiting: the reset runs on the server; check it with bash $MIG_DIR/quota-reset.sh status"
  exit 0
fi
start=$(date +%s)
while :; do
  s=$(queued StoreMaintenance) || s=error
  a=$(queued AccountMaintenance) || a=error
  printf 'queued StoreMaintenance=%s AccountMaintenance=%s after %ss\n' "$s" "$a" "$(( $(date +%s) - start ))"
  [ "$s" = 0 ] && [ "$a" = 0 ] && break
  [ $(( $(date +%s) - start )) -lt "$TIMEOUT" ] || die "quota tasks still queued after ${TIMEOUT}s"
  sleep 15
done
usage
