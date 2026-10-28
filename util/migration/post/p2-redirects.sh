#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
ZONE=${1:-${CF_ZONE_ID_TBPRO:-}}
[ -n "$ZONE" ] || die "usage: post/p2-redirects.sh [ZONE_ID] [--dry-run|--apply] (ZONE_ID defaults to CF_ZONE_ID_TBPRO)"
need CF_API_TOKEN_AGENT
REDIRECTS="accounts-stage.tb.pro=https://accounts.stage-thundermail.com auth-stage.tb.pro=https://auth.stage-thundermail.com"
EP=$CF_API/zones/$ZONE/rulesets/phases/http_request_dynamic_redirect/entrypoint

zone_name=$(cf_api "$CF_API/zones/$ZONE" | jq -re '.result.name')
[ "$zone_name" = tb.pro ] || die "zone $ZONE is $zone_name, expected tb.pro"

rules=$(for r in $REDIRECTS; do jq -nc --arg from "${r%%=*}" --arg to "${r#*=}" '{
  ref: ("p2_" + ($from | gsub("[.-]"; "_"))),
  description: ("P2 301 " + $from + "/* -> " + $to + "/$1"),
  expression: ("(http.host eq \"" + $from + "\")"),
  action: "redirect",
  action_parameters: {from_value: {status_code: 301, target_url: {expression: ("concat(\"" + $to + "\", http.request.uri.path)")}, preserve_query_string: true}}
}'; done | jq -sc '.')

before=$(mktemp --suffix=.json "$EVIDENCE_DIR/tbpro-rules-before-$TS-XXXX")
cf_api "$EP" > "$before"
if jq -e '.success' "$before" >/dev/null; then
  existing=$(jq -c '.result.rules // []' "$before")
elif jq -e '[.errors[]?.code] | index(10003)' "$before" >/dev/null; then
  existing='[]'
else
  die "GET entrypoint failed: $(jq -c '.errors' "$before")"
fi
body=$(jq -nc --argjson cur "$existing" --argjson new "$rules" '
  ($new | map(.ref)) as $refs
  | {rules: ([$cur[] | select(.ref as $r | $refs | index($r) | not)
      | {id, ref, description, expression, action, action_parameters, enabled} | with_entries(select(.value != null))] + $new)}')
new=$(jq 'length' <<<"$rules")
kept=$(jq --argjson n "$new" '.rules | length - $n' <<<"$body")
echo "PUT $EP"
jq . <<<"$body"

recs=""
for r in $REDIRECTS; do
  resp=$(cf_api "$CF_API/zones/$ZONE/dns_records?name=${r%%=*}")
  echo "$resp" | jq -e '.success' >/dev/null || die "dns_records ${r%%=*} failed: $(echo "$resp" | jq -c '.errors')"
  found=$(echo "$resp" | jq -c '.result[] | select(.type | IN("A","AAAA","CNAME")) | {id, name, type, proxied}')
  [ -n "$found" ] || die "no A, AAAA or CNAME record for ${r%%=*}"
  recs+="$found"$'\n'
done
jq -r '"\(if .proxied then "KEEP " else "PROXY" end) \(.type) \(.name) proxied=\(.proxied)"' <<<"$recs"
todo=$(jq -c 'select(.proxied | not)' <<<"$recs")
flips=$(grep -c . <<<"$todo" || true)

fail=0
if [ "$APPLY" = 1 ]; then
  r=$(cf_api -X PUT "$EP" --data "$body")
  jq -e '.success' <<<"$r" >/dev/null || { echo "PUT failed: $(jq -c '.errors' <<<"$r")"; fail=$((fail + 1)); }
  if [ "$fail" = 0 ]; then
    while IFS= read -r rec; do
      [ -n "$rec" ] || continue
      r=$(cf_api -X PATCH "$CF_API/zones/$ZONE/dns_records/$(jq -r .id <<<"$rec")" --data '{"proxied":true}')
      jq -e '.success' <<<"$r" >/dev/null || { echo "PATCH $(jq -r .name <<<"$rec") failed: $(jq -c '.errors' <<<"$r")"; fail=$((fail + 1)); }
    done <<<"$todo"
  fi
fi
printf 'mode=%s zone=%s rules_new=%s rules_kept=%s proxied_flips=%s failed=%s before=%s\n' "$(mode)" "$ZONE" "$new" "$kept" "$flips" "$fail" "$before"
[ "$fail" = 0 ]
