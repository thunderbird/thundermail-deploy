#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD KARGO_PROJECT_CONFIGS KARGO_STAGE_CONFIGS KUBECONFIG_TARGET KUBECONFIG_HUB SECRET_DIR
pending STALWART_ADMIN_ACCOUNT_ID TEST_USER
export STALWART_URL STALWART_USER STALWART_PASSWORD STALWART_ADMIN_ACCOUNT_ID
PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
S=$SECRET_DIR
PHASE=1
[ "$APPLY" = 1 ] && [ -n "$GO" ] && PHASE=2
PROMOTION=$EVIDENCE_DIR/promotion-configs-$TS.yaml
PROMOTION_RESTORE=$EVIDENCE_DIR/promotion-configs-restore$SUF-$TS.yaml
DROPPED_DOMAINS_ARG=()
[ "$TARGET_ENV" != tb-prod ] || DROPPED_DOMAINS_ARG=(--dropped-domains "$S/domains-dropped.json")
UNMIGRATED=("$PY" "$MIG_DIR/apply_unmigrated.py" --settings "$S/settings.json" --principals "$S/principals.json" --expect-hostname "$MAIL_HOST"
  ${DROPPED_DOMAINS_ARG[@]+"${DROPPED_DOMAINS_ARG[@]}"}
  --skip WebHook --skip AllowedIp --skip MtaSts --skip MtaQueueQuota --skip Security --skip Calendar --skip AddressBook --skip Http)

snap_settings() {
  local out
  if restore_staged; then echo "restore already staged for this cut, snapshot kept: $RESTORE"; return 0; fi
  if [ "$APPLY" = 1 ]; then
    out=$(bash "$MIG_DIR/snapshot-settings.sh") || return 1
  else
    printf '  + bash %s\n' "$MIG_DIR/snapshot-settings.sh"
    live_stalwart stalwart-cli || return 0
    out=$(bash "$MIG_DIR/snapshot-settings.sh" --dry-run) || return 1
  fi
  printf '%s\n' "$out"
  FREIGHT_BEFORE=$(tail -1 <<<"$out")
  SETTINGS_BEFORE=$(awk -v p="$S/settings-before-" 'index($0, p) == 1 && /\.ndjson$/ && !/ /' <<<"$out" | tail -1)
  [ -s "$FREIGHT_BEFORE" ] || { echo "no freight-before file"; return 1; }
  [ "$APPLY" = 0 ] || [ -s "$SETTINGS_BEFORE" ] || { echo "snapshot-settings.sh printed no settings-before file"; return 1; }
  local promote_back
  if [ "$(cat "$FREIGHT_BEFORE")" = none ]; then
    promote_back="# no Freight before W5: stage $KARGO_STAGE_CONFIGS had no Freight history ($FREIGHT_BEFORE holds none), so a restore promotes no Freight back"
    mark "$(sn freight-before)" freight none
  elif [ -e "$MIG_DIR/promotion.yaml.tpl" ]; then
    sed "s/__STAGE__/$KARGO_STAGE_CONFIGS/; s/__PROJECT__/$KARGO_PROJECT_CONFIGS/; s/__TS__/$(date -u +%s)r/; s/__FREIGHT__/$(cat "$FREIGHT_BEFORE")/" "$MIG_DIR/promotion.yaml.tpl" > "$PROMOTION_RESTORE" || return 1
  else
    printf 'promotion.yaml.tpl missing: render it with __FREIGHT__=%s\n' "$(cat "$FREIGHT_BEFORE")" > "$PROMOTION_RESTORE"
  fi
  printf '%s\n' \
    "${promote_back:-$(shq "${KH[@]}" create -f "$PROMOTION_RESTORE")}" \
    "stalwart-cli apply --file ${SETTINGS_BEFORE:-<settings-before file>} --dry-run" \
    "stalwart-cli apply --file ${SETTINGS_BEFORE:-<settings-before file>}" \
    "stalwart-cli create action/ReloadSettings" \
    "for ip in \$($(shq "${KS[@]}" -n thundermail get pod -l app=stalwart-nginx --field-selector=status.phase=Running -o 'jsonpath={.items[*].status.podIP}')); do ids=\$(stalwart-cli query BlockedIp --where \"address=\$ip\" --fields id --json | jq -r .id | paste -sd, -); [ -z \"\$ids\" ] || stalwart-cli delete BlockedIp --ids \"\$ids\"; done" \
    "stalwart-cli create action/ReloadBlockedIps" \
    "$(shq "${KS[@]}" -n thundermail rollout restart deploy/stalwart); $(shq "${KS[@]}" -n thundermail rollout status deploy/stalwart --timeout=600s)" > "$RESTORE" || return 1
  mark_restore
}

resolve_t6() {
  local sha pr=${T6_PR:-${T6_PR_DEFAULT:-}}
  [ -z "${T6_SHA:-}" ] || return 0
  [ -n "$pr" ] || { echo "T6_SHA and T6_PR are empty in $ENV_FILE"; return 1; }
  sha=$(gh pr view "$pr" -R thunderbird/thundermail-deploy --json mergeCommit -q '.mergeCommit.oid // empty') || return 1
  [ -n "$sha" ] || { echo "PR $pr has no merge commit"; return 1; }
  if [ -n "${T6_PR:-}" ]; then echo "T6_SHA from PR $pr: $sha"; else echo "T6_SHA from PR $pr (T6_PR_DEFAULT of targets/$TARGET_ENV.sh): $sha"; fi
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
  if [ "$fb" = none ]; then echo "stage Freight before W5: none (the stage had no Freight history)"; return 0; fi
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

quotas() {
  if [ "$TARGET_ENV" = tb-prod ]; then run bash "$MIG_DIR/quota-reset.sh" --apply --no-wait; else run bash "$MIG_DIR/quota-reset.sh" --apply; fi
}

quota_status() {
  local out rc=0
  printf '  + bash %s status\n' "$MIG_DIR/quota-reset.sh"
  [ "$APPLY" = 1 ] || return 0
  out=$(bash "$MIG_DIR/quota-reset.sh" status 2>&1) || rc=$?
  printf '%s\n' "$out"
  [ "$rc" = 0 ] || printf 'WAIT  the quota reset is not finished or not readable (quota-reset.sh status exit %s): W5 goes on; check it again before 7.8 with bash %s status\n' "$rc" "$MIG_DIR/quota-reset.sh"
  return 0
}

job_started() { [ "$("${KS[@]}" -n thundermail get job stalwart-config-apply -o jsonpath='{.status.startTime}' 2>/dev/null)" \> "$PROMOTE_AT" ]; }

promote() {
  if [ "$APPLY" = 0 ] && ! live promotion.yaml.tpl; then printf '  + %s\n' "$(shq "${KH[@]}" create -f "$PROMOTION")"; return 0; fi
  sed "s/__STAGE__/$KARGO_STAGE_CONFIGS/; s/__PROJECT__/$KARGO_PROJECT_CONFIGS/; s/__TS__/$(date -u +%s)/; s/__FREIGHT__/$FREIGHT/" "$MIG_DIR/promotion.yaml.tpl" > "$PROMOTION" || return 1
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
  local out f
  f=$S/unmigrated-plan-${1:-before}-$(mode)-$TS.txt
  out=$("${UNMIGRATED[@]}") || { printf '%s\n' "$out" | grep -E '^target:|FAILED|expected' ; return 1; }
  printf '%s\n' "$out" > "$f"
  BLOCKED=$(sed -n 's/^  BlockedIp: \([0-9]*\) to create.*/\1/p' <<<"$out" | tail -1)
  CATCHALL=$(grep -c '^  update catch-all ' <<<"$out" || true)
  OTHER=$(grep -E '^  (create|update) ' <<<"$out" | grep -vc '^  update catch-all ' || true)
  PLAN="blocked=${BLOCKED:-0} catchall=$CATCHALL other=$OTHER"
  echo "plan: $PLAN ($f)"
}

unmigrated_dry() {
  printf '  + %s\n' "$(shq "${UNMIGRATED[@]}")"
  [ "$APPLY" = 1 ] || live_stalwart apply_unmigrated.py "$S/settings.json" "$S/principals.json" stalwart-cli || return 0
  unmigrated_plan || return 1
  VALUE=$(( ${BLOCKED:-0} + CATCHALL + OTHER ))
  [ "$OTHER" = 0 ] || { echo "changes beyond BlockedIp and catch-all: the #1541 values are incomplete"; [ "$APPLY" = 0 ]; }
}

unmigrated_apply() {
  if [ "$APPLY" = 1 ] && [ -n "$(after_cut unmigrated-applied)" ]; then echo "apply_unmigrated.py --apply already done for this cut (unmigrated-applied row): skipped"; VALUE=0; return 0; fi
  unmigrated_dry || return 1
  [ "$APPLY" = 0 ] || [ "$PLAN" = "$APPROVED" ] || { echo "plan changed since phase 1: approved '$APPROVED', now '$PLAN'"; return 1; }
  run "${UNMIGRATED[@]}" --apply || return 1
  [ "$APPLY" = 1 ] || return 0
  unmigrated_plan after || return 1
  VALUE=$(( ${BLOCKED:-0} + CATCHALL + OTHER ))
  [ "$VALUE" = 0 ] || { echo "second dry run still reports $VALUE change(s)"; return 1; }
  mark unmigrated-applied changes 0
}

nginx_ips() { "${KS[@]}" -n thundermail get pod -l app=stalwart-nginx --field-selector=status.phase=Running -o jsonpath='{.items[*].status.podIP}'; }
banned_ids() { stalwart-cli query BlockedIp --where "address=$1" --fields id --json | jq -r 'if (.id | type) == "string" then .id else error("BlockedIp row without an id") end' | paste -sd, -; }

unban_nginx() {
  local ips ip ids all
  printf '  + stalwart-cli query BlockedIp --where address=<ip> --fields id --json, then stalwart-cli delete BlockedIp --ids <id> for each Running nginx pod IP listed\n'
  [ "$APPLY" = 1 ] || live_stalwart stalwart-cli || return 0
  ips=$(nginx_ips) || return 1
  [ -n "$ips" ] || { echo "no Running stalwart-nginx pod"; return 1; }
  VALUE=0
  for ip in $ips; do
    ids=$(banned_ids "$ip") || return 1
    [ -n "$ids" ] || continue
    VALUE=$((VALUE + 1))
    run stalwart-cli delete BlockedIp --ids "$ids" || return 1
  done
  all=$(stalwart-cli query BlockedIp --fields id --json) || return 1
  echo "BlockedIp entries=$(grep -c . <<<"$all" || true) nginx pod IPs banned=$VALUE"
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
  local ips ip ids n=0 unbanned=0
  [ "$APPLY" = 1 ] || live_stalwart apply_unmigrated.py "$S/settings.json" "$S/principals.json" stalwart-cli || return 0
  unmigrated_plan readback || return 1
  ips=$(nginx_ips) || return 1
  [ -n "$ips" ] || { echo "no Running stalwart-nginx pod"; return 1; }
  for ip in $ips; do
    ids=$(banned_ids "$ip") || return 1
    if [ -n "$ids" ]; then n=$((n + 1)); continue; fi
    if jq -e --arg k "server.blocked-ip.$ip" 'has($k)' "$S/settings.json" >/dev/null; then unbanned=$((unbanned + 1)); fi
  done
  VALUE=$(( ${BLOCKED:-0} - unbanned + CATCHALL + OTHER ))
  echo "read-back: unmigrated changes=$VALUE (besides $unbanned un-banned nginx pod IPs) nginx pod IPs banned=$n"
  [ "$APPLY" = 0 ] || { [ "$VALUE" = 0 ] && [ "$n" = 0 ]; }
}

accounts_check() {
  local out id ready cmd="printf 'user = \"%s:%s\"\\n' \"\$STALWART_JMAP_API_AUTH_USER\" \"\$STALWART_JMAP_API_AUTH_SECRET\" | curl -sk -m 20 -K - -w '\\n%{http_code}' \"\$STALWART_JMAP_API_URL/session\""
  printf '  + %s\n' "$(shq "${KS[@]}" -n accounts exec deploy/accounts-web -- sh -c "$cmd")"
  printf '  + a failure is PENDING (not a STOP) while %s has no put-accounts-bundle row for this cut\n' "$TIMING_TSV"
  [ "$APPLY" = 1 ] || return 0
  ready=$("${KS[@]}" -n accounts get deploy accounts-web -o jsonpath='{.status.readyReplicas}') || return 1
  if [ "${ready:-0}" = 0 ]; then
    VALUE=no-ready-pod
    printf 'PENDING  Accounts is not up yet: deploy/accounts-web has 0 ready replicas, so the Accounts -> Stalwart check runs after Accounts starts\n'
    NEEDS=$((NEEDS + 1))
    return 0
  fi
  out=$("${KS[@]}" -n accounts exec deploy/accounts-web -- sh -c "$cmd") || return 1
  VALUE=$(tail -1 <<<"$out")
  id=$(sed '$d' <<<"$out" | jq -r '.primaryAccounts["urn:stalwart:jmap"] // empty' 2>/dev/null || true)
  echo "Accounts -> Stalwart GET /jmap/session: $VALUE urn:stalwart:jmap account ${id:+present}${id:-missing}"
  [ "$VALUE" = 200 ] && [ -n "$id" ] && return 0
  [ "$VALUE" != 200 ] || VALUE=200-no-account
  [ -z "$(after_cut put-accounts-bundle)" ] || return 1
  printf 'PENDING  the #1539 Accounts bundle write: no put-accounts-bundle row for this cut in %s; write the bundle, rerun with --apply --go, and W7 --apply stops until this check is 200; record the row with: %s\n' "$TIMING_TSV" "printf 'put-accounts-bundle\\t%s\\t%s\\t\\t\\n' \"\$(date -u +%FT%TZ)\" \"\$(date -u +%FT%TZ)\" >> $(shq "$TIMING_TSV")"
  NEEDS=$((NEEDS + 1))
}

prereqs
helpers snapshot-settings.sh quota-reset.sh quota-reset.json.tpl apply_unmigrated.py promotion.yaml.tpl stalwart-cli "$PY" openssl ss timeout
if [ "$APPLY" = 1 ]; then port_forward || stop "no port-forward to Stalwart"; else port_forward || echo "no port-forward to Stalwart: live reads skipped"; fi
if [ "$PHASE" = 1 ]; then
  step snap-settings "" snap_settings
  step freight "" freight
  step quotas "" quotas
  step settings-promote succeeded promote
  step stalwart-restart "" restart_stalwart
  [ "$TARGET_ENV" != tb-prod ] || step quota-status "" quota_status
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
printf 'manual: create and delete one app password for %s through Accounts (manage.py shell in deploy/accounts-web), check both Stalwart calls 2xx in the accounts-web log, then create one more and store it with: bash %s --apply (reads TEST_SENDER_APP_PASSWORD from the operator env file, else stdin, checks JMAP 443, IMAP 993 and SMTP AUTH 587, appends it to %s)\n' "$TEST_USER" "$MIG_DIR/window/test-sender-password.sh" "$ENV_FILE"
printf '  + %s | grep -cE %s\n' "$(shq "${KS[@]}" -n accounts logs deploy/accounts-web --since=10m)" "'app.?password'"
finish
