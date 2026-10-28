#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need CF_ZONE_ID TARGET_NLB_DNS KARGO_PROJECT_CONFIGS KARGO_STAGE_CONFIGS KARGO_WAREHOUSE_CONFIGS KUBECONFIG_STAGE KUBECONFIG_HUB
pending FLIP_PR SPF_APEX_TXT SPF_FINAL_TXT
TMD=thunderbird/thundermail-deploy
PR_B=148
ZONE_NAME=stage-thundermail.com
MANUAL_PROMOTION=$EVIDENCE_DIR/promotion-configs-manual$SUF-$TS.yaml
PROMOTION=$EVIDENCE_DIR/promotion-configs-flip-$TS.yaml

next_step() { local n; for n in 1 2 3 4 5; do [ -n "$(after_cut "w7-step$n")" ] || { echo "$n"; return; }; done; echo 6; }

cf() {
  if [ "$APPLY" = 1 ]; then run bash "$MIG_DIR/cf-record.sh" "$CF_ZONE_ID" "$@" --apply; return; fi
  printf '  + %s\n' "$(shq bash "$MIG_DIR/cf-record.sh" "$CF_ZONE_ID" "$@" --apply)"
  if [ -n "${CF_API_TOKEN_AGENT:-}" ] && [[ "$*" != *"\${"* ]]; then bash "$MIG_DIR/cf-record.sh" "$CF_ZONE_ID" "$@" | sed 's/^/    plan: /'; fi
}

snap_zone() {
  local out snap realm
  if restore_staged; then echo "restore already staged for this cut: $RESTORE"; return 0; fi
  if [ "$APPLY" = 0 ] && [ -z "${CF_API_TOKEN_AGENT:-}" ]; then printf 'NEED  CF_API_TOKEN_AGENT for the zone snapshot\n  + bash %s %s\n' "$MIG_DIR/snapshot-zone.sh" "$CF_ZONE_ID"; NEEDS=$((NEEDS + 1)); return 0; fi
  out=$(bash "$MIG_DIR/snapshot-zone.sh" "$CF_ZONE_ID") || return 1
  printf '%s\n' "$out"
  snap=$(tail -1 <<<"$out")
  cf_api "$CF_API/zones/$CF_ZONE_ID/dns_records/export" > "$EVIDENCE_DIR/zone-bind-$CF_ZONE_ID-$TS.txt" || return 1
  VALUE=$(jq length "$snap")
  FREIGHT_MANUAL=$("${KH[@]}" -n "$KARGO_PROJECT_CONFIGS" get stage "$KARGO_STAGE_CONFIGS" -o jsonpath='{.status.freightHistory[0].items.*.name}') || return 1
  [ -n "$FREIGHT_MANUAL" ] || { echo "no Freight on $KARGO_STAGE_CONFIGS to roll back to"; return 1; }
  setenv FREIGHT_MANUAL "$FREIGHT_MANUAL" || return 1
  if [ -e "$MIG_DIR/promotion.yaml.tpl" ]; then
    sed "s/__TS__/$(date -u +%s)m/; s/__FREIGHT__/$FREIGHT_MANUAL/" "$MIG_DIR/promotion.yaml.tpl" > "$MANUAL_PROMOTION" || return 1
  else
    printf 'promotion.yaml.tpl missing: render it with __FREIGHT__=%s\n' "$FREIGHT_MANUAL" > "$MANUAL_PROMOTION"
  fi
  realm=$(find "$SECRET_DIR" -maxdepth 1 -name 'realm-before-*.json' -size +0 -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)
  printf '%s\n' \
    "0 since-flip counts before the rollback go and again right before step 4: bash $MIG_DIR/window/w7-since-flip.sh" \
    "1 legacy SG: the restore-legacy-sg.sh lines in $EVIDENCE_DIR/cut-legacy-restore.txt" \
    "2 promote the Manual Freight $FREIGHT_MANUAL while Stalwart is up: $(shq "${KH[@]}" create -f "$MANUAL_PROMOTION"); if the Job cannot run, apply the Manual render of that Freight's preview commit with stalwart-cli apply over the port-forward (stalwart-cli delete DnsServer fails while a Domain links it)" \
    "3 while tb-stage Keycloak is up: bash $MIG_DIR/restore-realm.sh ${realm:-<realm-before snapshot>} https://auth.stage-thundermail.com --dry-run, then --apply" \
    "4 $(shq "${KS[@]}" -n keycloak-customer scale sts/keycloak-customer --replicas=0); $(shq "${KS[@]}" -n accounts scale deploy/accounts-web deploy/accounts-celery deploy/accounts-flower --replicas=0); $(shq "${KS[@]}" -n thundermail scale deploy/stalwart --replicas=0)" \
    "5 legacy ECS: the restore-ecs.sh lines in $EVIDENCE_DIR/cut-legacy-restore.txt (Keycloak first, then Accounts)" \
    "6 bash $MIG_DIR/restore-zone.sh $snap --dry-run, then --apply" \
    "7 revert the FLIP_PR and PR $PR_B merge commits on TMD main (git revert -m 1 on a branch from main, merge commit); stay frozen (thaw PR unmerged) until Argo CD has synced the revert" \
    "8 revert the #1542 and #1543 repoints (pre-opened revert PRs, main -> env/tb-stage), restore-secret-version.sh, force-sync" > "$RESTORE" || return 1
  mark_restore
}

spf_edits() {
  cf delete "autoconfig.$ZONE_NAME" CNAME || return 1
  cf upsert "$ZONE_NAME" TXT "$SPF_APEX_TXT" || return 1
  cf upsert "spf.$ZONE_NAME" TXT "$SPF_FINAL_TXT"
}

es_ready() { [ "$("${KS[@]}" -n thundermail get externalsecret stalwart-cloudflare-api-token -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }

merge_b() {
  [ "$APPLY" = 0 ] || mark merge-148 pr "$PR_B"
  merge_pr "$TMD" "$PR_B" || return 1
  printf '  + wait 300s for externalsecret stalwart-cloudflare-api-token Ready\n'
  [ "$APPLY" = 1 ] || return 0
  until_ok 300 es_ready || { echo "ExternalSecret stalwart-cloudflare-api-token is not Ready after 300s"; return 1; }
}

job_started() { [ "$("${KS[@]}" -n thundermail get job stalwart-config-apply -o jsonpath='{.status.startTime}' 2>/dev/null)" \> "$PROMOTE_AT" ]; }

flip() {
  local sha
  merge_pr "$TMD" "$FLIP_PR" || return 1
  printf '  + FLIP_SHA from gh pr view FLIP_PR; wait 600s for freight_for FLIP_SHA; append FREIGHT; promote it; wait for job/stalwart-config-apply\n'
  [ "$APPLY" = 1 ] || return 0
  sha=$(gh pr view "$FLIP_PR" -R "$TMD" --json mergeCommit -q '.mergeCommit.oid // empty') || return 1
  [ -n "$sha" ] || { echo "FLIP_PR $FLIP_PR has no merge commit"; return 1; }
  until_ok 600 freight_for "$sha" || { echo "no Freight for the flip commit $sha after 600s: stop, FREIGHT not appended"; return 1; }
  FREIGHT=$FREIGHT_FOUND
  setenv FREIGHT "$FREIGHT" || return 1
  sed "s/__TS__/$(date -u +%s)/; s/__FREIGHT__/$FREIGHT/" "$MIG_DIR/promotion.yaml.tpl" > "$PROMOTION" || return 1
  PROMOTE_AT=$(date -u +%FT%TZ)
  "${KH[@]}" create -f "$PROMOTION" || return 1
  until_ok 120 job_started || { echo "stalwart-config-apply did not start after the promotion"; return 1; }
  "${KS[@]}" -n thundermail wait --for=condition=complete job/stalwart-config-apply --timeout=600s || return 1
  VALUE=$("${KS[@]}" -n thundermail get job stalwart-config-apply -o jsonpath='{.status.succeeded}')
  [ "$VALUE" = 1 ]
}

cname_live() { [ "$(curl -s -m 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=mail.$ZONE_NAME&type=CNAME" | jq -r '.Answer[0].data // empty' | sed 's/\.$//')" = "$TARGET_NLB_DNS" ]; }

mail_cname() {
  local start
  cf upsert "mail.$ZONE_NAME" CNAME "$TARGET_NLB_DNS" || return 1
  cf delete "_imap._tcp.$ZONE_NAME" SRV || return 1
  cf delete "_submission._tcp.$ZONE_NAME" SRV || return 1
  printf '  + wait 900s for DoH mail.%s CNAME = %s, then the dns-cutover row (start = the merge-148 row)\n' "$ZONE_NAME" "$TARGET_NLB_DNS"
  [ "$APPLY" = 1 ] || return 0
  until_ok 900 cname_live || { echo "mail.$ZONE_NAME does not resolve to $TARGET_NLB_DNS after 900s"; return 1; }
  start=$(after_cut merge-148 | cut -f2)
  row dns-cutover "${start:-unknown}" "$(date -u +%FT%TZ)" target "$TARGET_NLB_DNS"
}

verify() {
  local args=(--dry-run)
  [ "$APPLY" = 0 ] || args=(--apply)
  printf '  + bash %s %s\n' "$MIG_DIR/window/w7-verify.sh" "${args[*]}"
  [ "$APPLY" = 1 ] || return 0
  bash "$MIG_DIR/window/w7-verify.sh" "${args[@]}"
}

gated() {
  local n=$1 name=$2 fn=$3
  [ "$APPLY" = 0 ] || [ "$n" = "$NEXT" ] || return 0
  printf -- '-- W7 step %s\n' "$n"
  go_gate
  step "$name" "" "$fn"
  [ "$APPLY" = 0 ] || { mark "w7-step$n" status "$name"; echo "W7 step $n done; rerun with --apply --go '<operator words>' for the next step"; }
}

prereqs
helpers snapshot-zone.sh restore-zone.sh cf-record.sh promotion.yaml.tpl window/w7-verify.sh
NEXT=$(next_step)
if [ "$APPLY" = 1 ]; then
  for v in SPF_APEX_TXT SPF_FINAL_TXT; do case "${!v}" in "v=spf1 "*) ;; *) stop "$v does not begin with 'v=spf1 ' (#1535 decision)" ;; esac; done
fi
[ "$APPLY" = 0 ] || [ "$NEXT" != 6 ] || { echo "W7 steps 1-4 and verification are done for this cut"; finish; exit 0; }
[ "$APPLY" = 0 ] || [ "$NEXT" != 1 ] || step snap-zone records snap_zone
[ "$APPLY" = 1 ] || step snap-zone records snap_zone
gated 1 spf-edits spf_edits
gated 2 merge-148 merge_b
gated 3 flip-promote flip
gated 4 mail-cname mail_cname
[ "$APPLY" = 0 ] || [ "$NEXT" = 4 ] || [ "$NEXT" = 5 ] || { finish; exit 0; }
step w7-verify "" verify
[ "$APPLY" = 0 ] || mark w7-step5 status w7-verify
finish
