#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
ROLLBACK=0
POS=()
for a in "$@"; do case "$a" in --rollback) ROLLBACK=1 ;; *) POS+=("$a") ;; esac; done
ZONE=${POS[0]:-${CF_ZONE_ID_TBPRO:-}}
[[ $ZONE =~ ^[0-9a-f]{32}$ ]] || die "usage: post/p2-redirects.sh [ZONE_ID] [--rollback] [--dry-run|--apply] (ZONE_ID defaults to CF_ZONE_ID_TBPRO and must be a 32-hex zone id, got '$ZONE')"
need CF_API_TOKEN_AGENT
REDIRECTS="accounts-stage.tb.pro=https://accounts.stage-thundermail.com auth-stage.tb.pro=https://auth.stage-thundermail.com"
EP=$CF_API/zones/$ZONE/rulesets/phases/http_request_dynamic_redirect/entrypoint
RECS_DIR=$CF_API/zones/$ZONE/dns_records

ok() { jq -e '.success' <<<"$1" >/dev/null 2>&1; }
errs() { jq -c '.errors' <<<"$1" 2>/dev/null || head -c 160 <<<"$1"; }
write() { cf_api "$@" || echo '{"success":false,"errors":[{"message":"curl failed; outcome unknown, GET the current state before retrying"}]}'; }
lines() { grep -c . <<<"$1" || true; }

zr=$(cf_api "$CF_API/zones/$ZONE" || true)
zone_name=$(jq -r '.result.name // empty' <<<"$zr" 2>/dev/null || true)
[ "$zone_name" = tb.pro ] || die "zone $ZONE is '${zone_name:-?}', expected tb.pro: $(errs "$zr")"

before=$(mktemp --suffix=.json "$EVIDENCE_DIR/tbpro-rules-before-$TS-XXXX")
cf_api "$EP" > "$before" || true
echo "saved $before"
if ok "$(cat "$before")"; then
  existing=$(jq -c '.result.rules // []' "$before")
elif jq -e '[.errors[]?.code] | index(10003)' "$before" >/dev/null 2>&1; then
  existing='[]'
else
  die "GET entrypoint failed: $(errs "$(cat "$before")")"
fi
fail=0

if [ "$ROLLBACK" = 1 ]; then
  shopt -s nullglob
  files=("$EVIDENCE_DIR"/tbpro-p2-records-before-*.json)
  todo=""
  if [ ${#files[@]} = 0 ]; then echo "no $EVIDENCE_DIR/tbpro-p2-records-before-*.json, no records to unproxy"
  else todo=$(jq -sc 'add | group_by(.id) | map(map(select(.proxied == false)) | first | select(. != null)) | .[]' "${files[@]}"); fi
  rsid=$(jq -r '.result.id // empty' "$before")
  dels=$(jq -c '.[] | select((.ref // "") | startswith("p2_")) | {id, ref}' <<<"$existing")
  [ -z "$dels" ] || [ ${#files[@]} -gt 0 ] || die "live p2_ rules found but no $EVIDENCE_DIR/tbpro-p2-records-before-*.json: copy the saved records here or unproxy the two records by hand before deleting the rules"
  [ -z "$todo" ] || jq -r --arg base "$RECS_DIR" '"PATCH \($base)/\(.id) {\"proxied\":false,\"ttl\":\(.ttl)} (\(.name))"' <<<"$todo"
  [ -z "$dels" ] || jq -r --arg base "$CF_API/zones/$ZONE/rulesets/$rsid/rules" '"DELETE \($base)/\(.id) (\(.ref))"' <<<"$dels"
  if [ "$APPLY" = 1 ]; then
    while IFS= read -r rec; do
      [ -n "$rec" ] || continue
      r=$(write -X PATCH "$RECS_DIR/$(jq -r .id <<<"$rec")" --data "$(jq -c '{proxied: false, ttl}' <<<"$rec")")
      ok "$r" || { echo "PATCH $(jq -r .name <<<"$rec") failed: $(errs "$r")"; fail=$((fail + 1)); }
    done <<<"$todo"
    if [ "$fail" = 0 ]; then
      while IFS= read -r rule; do
        [ -n "$rule" ] || continue
        r=$(write -X DELETE "$CF_API/zones/$ZONE/rulesets/$rsid/rules/$(jq -r .id <<<"$rule")")
        ok "$r" || { echo "DELETE $(jq -r .ref <<<"$rule") failed: $(errs "$r")"; fail=$((fail + 1)); }
      done <<<"$dels"
    fi
  fi
  printf 'mode=%s rollback zone=%s unproxy=%s rules_deleted=%s failed=%s before=%s\n' "$(mode)" "$ZONE" "$(lines "$todo")" "$(lines "$dels")" "$fail" "$before"
  [ "$fail" = 0 ]
  exit
fi

rules=$(for r in $REDIRECTS; do jq -nc --arg from "${r%%=*}" --arg to "${r#*=}" '{
  ref: ("p2_" + ($from | gsub("[.-]"; "_"))),
  description: ("P2 301 " + $from + "/* -> " + $to + "/$1"),
  expression: ("(http.host eq \"" + $from + "\")"),
  action: "redirect",
  action_parameters: {from_value: {status_code: 301, target_url: {expression: ("concat(\"" + $to + "\", http.request.uri.path)")}, preserve_query_string: true}}
}'; done | jq -sc '.')
body=$(jq -nc --argjson cur "$existing" --argjson new "$rules" '
  ($new | map(.ref)) as $refs
  | {rules: ($new + [$cur[] | select(.ref as $r | $refs | index($r) | not)
      | {id, ref, description, expression, action, action_parameters, enabled} | with_entries(select(.value != null))])}')
new=$(jq 'length' <<<"$rules")
kept=$(jq --argjson n "$new" '.rules | length - $n' <<<"$body")
echo "PUT $EP"
jq . <<<"$body"

recs=""
for r in $REDIRECTS; do
  resp=$(cf_api "$RECS_DIR?name=${r%%=*}" || true)
  ok "$resp" || die "dns_records ${r%%=*} failed: $(errs "$resp")"
  found=$(jq -c '.result[] | select(.type | IN("A","AAAA","CNAME")) | {id, name, type, proxied, ttl}' <<<"$resp")
  [ -n "$found" ] || die "no A, AAAA or CNAME record for ${r%%=*}"
  recs+="$found"$'\n'
done
jq -r '"\(if .proxied then "KEEP " else "PROXY" end) \(.type) \(.name) proxied=\(.proxied)"' <<<"$recs"
todo=$(jq -c 'select(.proxied | not)' <<<"$recs")
flips=$(lines "$todo")
[ -z "$todo" ] || jq -r --arg base "$RECS_DIR" '"PATCH \($base)/\(.id) {\"proxied\":true}"' <<<"$todo"

if [ "$APPLY" = 1 ]; then
  saved=$(mktemp --suffix=.json "$EVIDENCE_DIR/tbpro-p2-records-before-$TS-XXXX")
  jq -s '.' <<<"$recs" > "$saved"
  echo "saved $saved"
  r=$(write -X PUT "$EP" --data "$body")
  ok "$r" || { echo "PUT failed: $(errs "$r")"; fail=$((fail + 1)); }
  if [ "$fail" = 0 ]; then
    while IFS= read -r rec; do
      [ -n "$rec" ] || continue
      r=$(write -X PATCH "$RECS_DIR/$(jq -r .id <<<"$rec")" --data '{"proxied":true}')
      ok "$r" || { echo "PATCH $(jq -r .name <<<"$rec") failed: $(errs "$r")"; fail=$((fail + 1)); }
    done <<<"$todo"
  fi
fi
printf 'mode=%s zone=%s rules_new=%s rules_kept=%s proxied_flips=%s failed=%s before=%s\n' "$(mode)" "$ZONE" "$new" "$kept" "$flips" "$fail" "$before"
[ "$fail" = 0 ]
