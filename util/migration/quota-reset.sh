#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD STALWART_ADMIN_ACCOUNT_ID
TIMEOUT=${QUOTA_RESET_TIMEOUT:-600}
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
queued() { call x:Task/query "$(jq -cn --arg t "$1" '{filter: {"@type": $t}, limit: 5000}')" | jq -r '.ids | length'; }
usage() {
  local ids
  ids=$(call x:Account/query '{"limit": 5000}' | jq -c '.ids')
  call x:Account/get "$(jq -cn --argjson ids "$ids" '{ids: $ids, properties: ["usedDiskQuota"]}')" | jq -r '"accounts=\(.list | length) with_usage=\([.list[] | select((.usedDiskQuota // 0) > 0)] | length) used_bytes=\([.list[].usedDiskQuota // 0] | add // 0)"'
}
now=$(date -u +%FT%TZ)
body=$(sed "s/__ACCOUNT_ID__/$STALWART_ADMIN_ACCOUNT_ID/; s/__NOW__/$now/g" "$MIG_DIR/quota-reset.json.tpl")
jq -e '.methodCalls[0][1].create["quota-reset"].maintenanceType == "resetUserQuotas"' <<<"$body" >/dev/null || die "quota-reset.json.tpl did not render"
if [ "$APPLY" != 1 ]; then
  printf 'dry-run: POST %s x:Task/set StoreMaintenance resetUserQuotas, accountId %s, due %s\n' "$API" "$STALWART_ADMIN_ACCOUNT_ID" "$now"
  printf 'dry-run: then poll x:Task/query StoreMaintenance and AccountMaintenance until both are 0 (timeout %ss), then x:Account/get usedDiskQuota\n' "$TIMEOUT"
  exit 0
fi
r=$(jmap "$body" | jq -ce '.methodResponses[0] | if .[0] == "error" then error(.[1] | tostring) else .[1] end') || die "x:Task/set failed"
jq -e '.created["quota-reset"].id' <<<"$r" >/dev/null || die "quota reset task not created: $(jq -c '.notCreated' <<<"$r")"
echo "created task $(jq -r '.created["quota-reset"].id' <<<"$r") due $now"
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
