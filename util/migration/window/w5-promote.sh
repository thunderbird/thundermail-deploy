#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD KARGO_PROJECT_CONFIGS KARGO_STAGE_CONFIGS KUBECONFIG_STAGE KUBECONFIG_HUB SECRET_DIR
pending STALWART_ADMIN_ACCOUNT_ID T6_SHA TEST_USER
export STALWART_URL STALWART_USER STALWART_PASSWORD STALWART_ADMIN_ACCOUNT_ID
PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
S=$SECRET_DIR
KH=(kubectl --kubeconfig "$KUBECONFIG_HUB")
PHASE=1
[ "$APPLY" = 1 ] && [ -n "$GO" ] && PHASE=2
PROMOTION=$EVIDENCE_DIR/promotion-configs-$TS.yaml
UNMIGRATED=("$PY" "$MIG_DIR/apply_unmigrated.py" --settings "$S/settings.json" --principals "$S/principals.json" --expect-hostname mail.stage-thundermail.com
  --skip WebHook --skip AllowedIp --skip MtaSts --skip MtaQueueQuota --skip Security --skip Calendar --skip AddressBook --skip Http)

snap_settings() {
  local out
  if [ "$APPLY" = 1 ]; then
    out=$(bash "$MIG_DIR/snapshot-settings.sh") || return 1
  else
    printf '  + bash %s\n' "$MIG_DIR/snapshot-settings.sh"
    live stalwart-cli || return 0
    out=$(bash "$MIG_DIR/snapshot-settings.sh" --dry-run) || return 1
  fi
  printf '%s\n' "$out"
  FREIGHT_BEFORE=$(tail -1 <<<"$out")
  SETTINGS_BEFORE=$(grep 'settings-before-' <<<"$out" | tail -1 || true)
  [ -s "$FREIGHT_BEFORE" ] || { echo "no freight-before file"; return 1; }
  printf '%s\n' \
    "$(shq "${KH[@]}" create -f "<promotion.yaml.tpl rendered with __FREIGHT__=$(cat "$FREIGHT_BEFORE")>")" \
    "stalwart-cli apply --file ${SETTINGS_BEFORE:-<settings-before file>} --dry-run" \
    "stalwart-cli apply --file ${SETTINGS_BEFORE:-<settings-before file>}" \
    "stalwart-cli create action/ReloadSettings" > "$RESTORE"
}

quotas() { run bash "$MIG_DIR/quota-reset.sh"; }

freight() {
  local f
  f=$("${KH[@]}" -n "$KARGO_PROJECT_CONFIGS" get freight --sort-by=.metadata.creationTimestamp -o json | jq -r --arg sha "$T6_SHA" '.items[] | select(.commits[0].id == $sha) | .metadata.name' | tail -1) || return 1
  echo "Freight for T6_SHA: ${f:-none}"
  [ -n "$f" ] || [ "$APPLY" = 0 ] || return 1
  FREIGHT=${f:-\$\{FREIGHT\}}
  setenv FREIGHT "$FREIGHT"
}

job_started() { [ "$("${KS[@]}" -n thundermail get job stalwart-config-apply -o jsonpath='{.status.startTime}' 2>/dev/null)" \> "$PROMOTE_AT" ]; }

promote() {
  if [ "$APPLY" = 0 ] && ! live promotion.yaml.tpl; then printf '  + %s\n' "$(shq "${KH[@]}" create -f "$PROMOTION")"; return 0; fi
  sed "s/__TS__/$(date -u +%s)/; s/__FREIGHT__/$FREIGHT/" "$MIG_DIR/promotion.yaml.tpl" > "$PROMOTION" || return 1
  sed 's/^/    /' "$PROMOTION"
  PROMOTE_AT=$(date -u +%FT%TZ)
  run "${KH[@]}" create -f "$PROMOTION" || return 1
  printf '  + wait 120s for job/stalwart-config-apply startTime > %s\n' "$PROMOTE_AT"
  if [ "$APPLY" = 1 ]; then until_ok 120 job_started || { echo "stalwart-config-apply did not start after the promotion"; return 1; }; fi
  run "${KS[@]}" -n thundermail wait --for=condition=complete job/stalwart-config-apply --timeout=600s || return 1
  [ "$APPLY" = 1 ] || return 0
  VALUE=$("${KS[@]}" -n thundermail get job stalwart-config-apply -o jsonpath='{.status.succeeded}')
  [ "$VALUE" = 1 ]
}

unmigrated_plan() {
  local out
  out=$("${UNMIGRATED[@]}") || { printf '%s\n' "$out" | grep -E '^target:|FAILED|expected' ; return 1; }
  printf '%s\n' "$out" > "$S/unmigrated-plan-$(mode)-$TS.txt"
  BLOCKED=$(sed -n 's/^  BlockedIp: \([0-9]*\) to create.*/\1/p' <<<"$out" | tail -1)
  CATCHALL=$(grep -c '^  update catch-all ' <<<"$out" || true)
  OTHER=$(grep -E '^  (create|update) ' <<<"$out" | grep -vc '^  update catch-all ' || true)
  echo "plan: BlockedIp to create=${BLOCKED:-0} catch-all updates=$CATCHALL other changes=$OTHER ($S/unmigrated-plan-$(mode)-$TS.txt)"
}

unmigrated_dry() {
  printf '  + %s\n' "$(shq "${UNMIGRATED[@]}")"
  [ "$APPLY" = 1 ] || live apply_unmigrated.py "$S/settings.json" "$S/principals.json" stalwart-cli || return 0
  unmigrated_plan || return 1
  VALUE=$(( ${BLOCKED:-0} + CATCHALL + OTHER ))
  [ "$OTHER" = 0 ] || { echo "changes beyond BlockedIp and catch-all: the #1541 values are incomplete"; [ "$APPLY" = 0 ]; }
}

unmigrated_apply() {
  unmigrated_dry || return 1
  run "${UNMIGRATED[@]}" --apply || return 1
  [ "$APPLY" = 1 ] || return 0
  unmigrated_plan || return 1
  VALUE=$(( ${BLOCKED:-0} + CATCHALL + OTHER ))
  [ "$VALUE" = 0 ] || { echo "second dry run still reports $VALUE change(s)"; return 1; }
}

unban_nginx() {
  local ips ip banned
  printf '  + stalwart-cli query BlockedIp, then stalwart-cli delete BlockedIp/<ip> for each nginx pod IP listed\n'
  [ "$APPLY" = 1 ] || live stalwart-cli || return 0
  ips=$("${KS[@]}" -n thundermail get pod -l app=stalwart-nginx -o jsonpath='{.items[*].status.podIP}') || return 1
  banned=$(stalwart-cli query BlockedIp) || return 1
  VALUE=0
  for ip in $ips; do
    grep -qwF "$ip" <<<"$banned" || continue
    VALUE=$((VALUE + 1))
    run stalwart-cli delete "BlockedIp/$ip" || return 1
  done
  echo "BlockedIp entries=$(grep -c . <<<"$banned" || true) nginx pod IPs banned=$VALUE"
}

reload() {
  run stalwart-cli create action/ReloadSettings || return 1
  run stalwart-cli create action/ReloadBlockedIps
}

accounts_check() {
  local cmd="curl -s -o /dev/null -w '%{http_code}\n' -u \"\$STALWART_JMAP_API_AUTH_USER:\$STALWART_JMAP_API_AUTH_SECRET\" \"\$STALWART_JMAP_API_URL/api/principal?limit=1\""
  printf '  + %s\n' "$(shq "${KS[@]}" -n accounts exec deploy/accounts-web -- sh -c "$cmd")"
  [ "$APPLY" = 1 ] || return 0
  VALUE=$("${KS[@]}" -n accounts exec deploy/accounts-web -- sh -c "$cmd") || return 1
  echo "Accounts -> Stalwart GET /api/principal: $VALUE"
  [ "$VALUE" = 200 ]
}

prereqs
helpers snapshot-settings.sh quota-reset.sh quota-reset.json.tpl apply_unmigrated.py promotion.yaml.tpl stalwart-cli "$PY"
if [ "$APPLY" = 1 ]; then port_forward || stop "no port-forward to Stalwart"; else port_forward || echo "no port-forward to Stalwart: live reads skipped"; fi
if [ "$PHASE" = 1 ]; then
  step snap-settings "" snap_settings
  step quotas "" quotas
  step freight "" freight
  step settings-promote succeeded promote
  step unmigrated-dry-run changes unmigrated_dry
  if [ "$APPLY" = 1 ]; then
    printf 'waiting for the operator: paste the plan counts above, then rerun with --apply --go "<operator words>"\n'
    finish
    exit 0
  fi
fi
go_gate
step unmigrated-apply changes unmigrated_apply
step unban-nginx banned unban_nginx
step reload "" reload
step accounts-stalwart-check http_code accounts_check
printf 'manual: create and delete one app password for %s through Accounts (manage.py shell in deploy/accounts-web), check both Stalwart calls 2xx in the accounts-web log, then create one more and append TEST_SENDER_APP_PASSWORD to %s\n' "$TEST_USER" "$ENV_FILE"
printf '  + %s | grep -cE %s\n' "$(shq "${KS[@]}" -n accounts logs deploy/accounts-web --since=10m)" "'app.?password'"
finish
