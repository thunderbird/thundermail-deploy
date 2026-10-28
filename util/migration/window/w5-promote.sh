#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD KARGO_PROJECT_CONFIGS KARGO_STAGE_CONFIGS KUBECONFIG_STAGE KUBECONFIG_HUB SECRET_DIR
pending STALWART_ADMIN_ACCOUNT_ID TEST_USER
export STALWART_URL STALWART_USER STALWART_PASSWORD STALWART_ADMIN_ACCOUNT_ID
PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
S=$SECRET_DIR
PHASE=1
[ "$APPLY" = 1 ] && [ -n "$GO" ] && PHASE=2
PROMOTION=$EVIDENCE_DIR/promotion-configs-$TS.yaml
PROMOTION_RESTORE=$EVIDENCE_DIR/promotion-configs-restore$SUF-$TS.yaml
UNMIGRATED=("$PY" "$MIG_DIR/apply_unmigrated.py" --settings "$S/settings.json" --principals "$S/principals.json" --expect-hostname mail.stage-thundermail.com
  --skip WebHook --skip AllowedIp --skip MtaSts --skip MtaQueueQuota --skip Security --skip Calendar --skip AddressBook --skip Http)

snap_settings() {
  local out
  if restore_staged; then echo "restore already staged for this cut, snapshot kept: $RESTORE"; return 0; fi
  if [ "$APPLY" = 1 ]; then
    out=$(bash "$MIG_DIR/snapshot-settings.sh") || return 1
  else
    printf '  + bash %s\n' "$MIG_DIR/snapshot-settings.sh"
    live stalwart-cli || return 0
    out=$(bash "$MIG_DIR/snapshot-settings.sh" --dry-run) || return 1
  fi
  printf '%s\n' "$out"
  FREIGHT_BEFORE=$(tail -1 <<<"$out")
  SETTINGS_BEFORE=$(awk -v p="$S/settings-before-" 'index($0, p) == 1 && /\.ndjson$/ && !/ /' <<<"$out" | tail -1)
  [ -s "$FREIGHT_BEFORE" ] || { echo "no freight-before file"; return 1; }
  [ "$APPLY" = 0 ] || [ -s "$SETTINGS_BEFORE" ] || { echo "snapshot-settings.sh printed no settings-before file"; return 1; }
  if [ -e "$MIG_DIR/promotion.yaml.tpl" ]; then
    sed "s/__TS__/$(date -u +%s)r/; s/__FREIGHT__/$(cat "$FREIGHT_BEFORE")/" "$MIG_DIR/promotion.yaml.tpl" > "$PROMOTION_RESTORE" || return 1
  else
    printf 'promotion.yaml.tpl missing: render it with __FREIGHT__=%s\n' "$(cat "$FREIGHT_BEFORE")" > "$PROMOTION_RESTORE"
  fi
  printf '%s\n' \
    "$(shq "${KH[@]}" create -f "$PROMOTION_RESTORE")" \
    "stalwart-cli apply --file ${SETTINGS_BEFORE:-<settings-before file>} --dry-run" \
    "stalwart-cli apply --file ${SETTINGS_BEFORE:-<settings-before file>}" \
    "stalwart-cli create action/ReloadSettings" \
    "stalwart-cli create action/ReloadBlockedIps" > "$RESTORE" || return 1
  mark_restore
}

resolve_t6() {
  local sha
  [ -z "${T6_SHA:-}" ] || return 0
  [ -n "${T6_PR:-}" ] || { echo "T6_SHA and T6_PR are empty in $ENV_FILE"; return 1; }
  sha=$(gh pr view "$T6_PR" -R thunderbird/thundermail-deploy --json mergeCommit -q '.mergeCommit.oid // empty') || return 1
  [ -n "$sha" ] || { echo "PR $T6_PR has no merge commit"; return 1; }
  echo "T6_SHA from PR $T6_PR: $sha"
  T6_SHA=$sha
  setenv T6_SHA "$sha"
}

no_freight() {
  echo "$1"
  [ "$APPLY" = 0 ] || return 1
  printf 'NEED  Freight for T6_SHA\n'
  NEEDS=$((NEEDS + 1))
  FREIGHT="\${FREIGHT}"
}

created() { "${KH[@]}" -n "$KARGO_PROJECT_CONFIGS" get freight "$1" -o jsonpath='{.metadata.creationTimestamp}'; }

not_older() {
  local fb="" file a b
  file=${FREIGHT_BEFORE:-$(find "$EVIDENCE_DIR" -maxdepth 1 -name 'freight-before-*.txt' -size +0 -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)}
  [ ! -s "$file" ] || fb=$(cat "$file")
  if [ -z "$fb" ] || [ "$fb" = "$1" ]; then echo "stage Freight before W5: ${fb:-unknown}"; return 0; fi
  a=$(created "$1") && b=$(created "$fb") || return 1
  echo "stage Freight before W5: $fb ($b); T6 match: $1 ($a)"
  [ "$a" \> "$b" ] || [ "$a" = "$b" ]
}

freight() {
  resolve_t6 || { no_freight "T6_SHA unresolved (merge commit of the #1541 values PR)"; return; }
  freight_for "$T6_SHA" || { no_freight "no Freight to promote for T6_SHA"; return; }
  not_older "$FREIGHT_FOUND" || { no_freight "the stage runs a newer Freight than the T6 match: promoting $FREIGHT_FOUND would roll it back"; return; }
  FREIGHT=$FREIGHT_FOUND
  setenv FREIGHT "$FREIGHT"
}

quotas() { run bash "$MIG_DIR/quota-reset.sh" --apply; }

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
  PLAN="blocked=${BLOCKED:-0} catchall=$CATCHALL other=$OTHER"
  echo "plan: $PLAN ($S/unmigrated-plan-$(mode)-$TS.txt)"
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
  [ "$APPLY" = 0 ] || [ "$PLAN" = "$APPROVED" ] || { echo "plan changed since phase 1: approved '$APPROVED', now '$PLAN'"; return 1; }
  run "${UNMIGRATED[@]}" --apply || return 1
  [ "$APPLY" = 1 ] || return 0
  unmigrated_plan || return 1
  VALUE=$(( ${BLOCKED:-0} + CATCHALL + OTHER ))
  [ "$VALUE" = 0 ] || { echo "second dry run still reports $VALUE change(s)"; return 1; }
}

nginx_ips() { "${KS[@]}" -n thundermail get pod -l app=stalwart-nginx -o jsonpath='{.items[*].status.podIP}'; }
banned_ids() { stalwart-cli query BlockedIp --where "address=$1" --json | jq -r '.id' | paste -sd, -; }

unban_nginx() {
  local ips ip ids
  printf '  + stalwart-cli query BlockedIp --where address=<ip> --json, then stalwart-cli delete BlockedIp --ids <id> for each nginx pod IP listed\n'
  [ "$APPLY" = 1 ] || live stalwart-cli || return 0
  ips=$(nginx_ips) || return 1
  VALUE=0
  for ip in $ips; do
    ids=$(banned_ids "$ip") || return 1
    [ -n "$ids" ] || continue
    VALUE=$((VALUE + 1))
    run stalwart-cli delete BlockedIp --ids "$ids" || return 1
  done
  echo "BlockedIp entries=$(stalwart-cli query BlockedIp --json | grep -c . || true) nginx pod IPs banned=$VALUE"
}

reload() {
  local a out
  for a in ReloadSettings ReloadBlockedIps; do
    printf '  + stalwart-cli create action/%s\n' "$a"
    [ "$APPLY" = 1 ] || continue
    out=$(stalwart-cli create "action/$a") || return 1
    printf '%s\n' "$out"
    grep -q '^Created Action ' <<<"$out" || { echo "action/$a: no Created Action reply"; return 1; }
  done
}

readback() {
  local ips ip ids n=0
  [ "$APPLY" = 1 ] || live apply_unmigrated.py "$S/settings.json" "$S/principals.json" stalwart-cli || return 0
  unmigrated_plan || return 1
  VALUE=$(( ${BLOCKED:-0} + CATCHALL + OTHER ))
  ips=$(nginx_ips) || return 1
  for ip in $ips; do
    ids=$(banned_ids "$ip") || return 1
    [ -z "$ids" ] || n=$((n + 1))
  done
  echo "read-back: unmigrated changes=$VALUE nginx pod IPs banned=$n"
  [ "$APPLY" = 0 ] || { [ "$VALUE" = 0 ] && [ "$n" = 0 ]; }
}

accounts_check() {
  local out id cmd="printf 'user = \"%s:%s\"\\n' \"\$STALWART_JMAP_API_AUTH_USER\" \"\$STALWART_JMAP_API_AUTH_SECRET\" | curl -sk -m 20 -K - -w '\\n%{http_code}' \"\$STALWART_JMAP_API_URL/session\""
  printf '  + %s\n' "$(shq "${KS[@]}" -n accounts exec deploy/accounts-web -- sh -c "$cmd")"
  [ "$APPLY" = 1 ] || return 0
  out=$("${KS[@]}" -n accounts exec deploy/accounts-web -- sh -c "$cmd") || return 1
  VALUE=$(tail -1 <<<"$out")
  id=$(sed '$d' <<<"$out" | jq -r '.primaryAccounts["urn:stalwart:jmap"] // empty' 2>/dev/null || true)
  echo "Accounts -> Stalwart GET /jmap/session: $VALUE urn:stalwart:jmap account ${id:+present}${id:-missing}"
  [ "$VALUE" = 200 ] && [ -n "$id" ]
}

prereqs
helpers snapshot-settings.sh quota-reset.sh quota-reset.json.tpl apply_unmigrated.py promotion.yaml.tpl stalwart-cli "$PY"
if [ "$APPLY" = 1 ]; then port_forward || stop "no port-forward to Stalwart"; else port_forward || echo "no port-forward to Stalwart: live reads skipped"; fi
if [ "$PHASE" = 1 ]; then
  step snap-settings "" snap_settings
  step freight "" freight
  step quotas "" quotas
  step settings-promote succeeded promote
  step unmigrated-dry-run changes unmigrated_dry
  if [ "$APPLY" = 1 ]; then
    mark unmigrated-plan counts "$PLAN"
    printf 'waiting for the operator: paste the plan counts above (%s), then rerun with --apply --go "<operator words>"\n' "$PLAN"
    finish
    exit 0
  fi
fi
if [ "$APPLY" = 1 ]; then
  APPROVED=$(after_cut unmigrated-plan | cut -f5)
  [ -n "$APPROVED" ] || stop "no unmigrated-plan row for this cut: run phase 1 (--apply without --go) first"
  [ -n "$(after_cut restore-W5)" ] || stop "no settings restore staged for this cut: run phase 1 first"
fi
go_gate
step unmigrated-apply changes unmigrated_apply
step unban-nginx banned unban_nginx
step reload "" reload
step readback changes readback
step accounts-stalwart-check http_code accounts_check
printf 'manual: create and delete one app password for %s through Accounts (manage.py shell in deploy/accounts-web), check both Stalwart calls 2xx in the accounts-web log, then create one more and store it with: bash %s --apply (reads it from stdin, checks a JMAP session, appends TEST_SENDER_APP_PASSWORD to %s)\n' "$TEST_USER" "$MIG_DIR/window/test-sender-password.sh" "$ENV_FILE"
printf '  + %s | grep -cE %s\n' "$(shq "${KS[@]}" -n accounts logs deploy/accounts-web --since=10m)" "'app.?password'"
finish
