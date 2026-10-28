#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need ARGO_APPS KARGO_PROJECT_ACCOUNTS KARGO_STAGE_ACCOUNTS KUBECONFIG_STAGE KUBECONFIG_HUB PI_DIR FREEZE_PR
pending DECISIONS_COMMENT_URL
PI=thunderbird/platform-infrastructure
THAW=$MIG_DIR/thaw.sh
[ -e "$THAW" ] || THAW=$MIG_DIR/window/thaw.sh

decisions() {
  printf '  + gh api repos/%s/issues/comments/%s --jq .body | grep -q %s\n' "$PI" "${DECISIONS_COMMENT_URL##*-}" "'^## Decisions and forks'"
  case "$DECISIONS_COMMENT_URL" in *issuecomment-*) ;; *) [ "$APPLY" = 0 ] && return 0; echo "DECISIONS_COMMENT_URL is not an issue comment URL"; return 1 ;; esac
  gh api "repos/$PI/issues/comments/${DECISIONS_COMMENT_URL##*-}" --jq .body | grep -q '^## Decisions and forks' || { echo "the Decisions and forks comment was not found"; return 1; }
}

snap() {
  local out
  out=$(bash "$MIG_DIR/snapshot-gitops.sh") || return 1
  printf '%s\n' "$out"
  REPLICAS_BEFORE=$(tail -1 <<<"$out")
  [ -s "$REPLICAS_BEFORE" ] || { echo "snapshot-gitops.sh wrote no replicas file"; return 1; }
  VALUE=$(grep -c . "$REPLICAS_BEFORE")
  setenv REPLICAS_BEFORE "$REPLICAS_BEFORE" || return 1
  if restore_staged; then echo "restore already staged for this cut: $RESTORE"; return 0; fi
  printf '%s\n' \
    "gh pr merge \"\$THAW_PR\" -R $PI --merge" \
    "bash $MIG_DIR/restore-replicas.sh $REPLICAS_BEFORE --dry-run" \
    "bash $MIG_DIR/restore-replicas.sh $REPLICAS_BEFORE --apply" > "$RESTORE" || return 1
  mark_restore
}

app_rev() { "${KH[@]}" -n argocd get application tb-stage-app-of-apps -o jsonpath='{.status.sync.revision}'; }
synced_to() { local r; r=$(app_rev) && [ -n "$r" ] && git -C "$PI_DIR" fetch -q origin main && git -C "$PI_DIR" merge-base --is-ancestor "$1" "$r"; }
self_heal() { local a; for a in $ARGO_APPS; do [ "$("${KH[@]}" -n argocd get application "$a" -o jsonpath='{.spec.syncPolicy.automated.selfHeal}')" = "$1" ] || return 1; done; }
auto_promote() { [ "$("${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get projectconfig "$KARGO_PROJECT_ACCOUNTS" -o jsonpath="{.spec.promotionPolicies[?(@.stage==\"$KARGO_STAGE_ACCOUNTS\")].autoPromotionEnabled}")" = "$1" ]; }
open_promotions() { "${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get promotions -o json | jq -r --arg s "$KARGO_STAGE_ACCOUNTS" '.items[] | select(.spec.stage == $s) | select((.status.phase // "Pending") | IN("Succeeded", "Failed", "Errored", "Aborted") | not) | .metadata.name'; }

freeze() {
  local open
  run gh pr merge "$FREEZE_PR" -R "$PI" --merge || return 1
  printf '  + bash %s, then reload %s for FREEZE_SHA and THAW_PR\n' "$THAW" "$ENV_FILE"
  printf '  + wait 400s each: app-of-apps sync revision contains FREEZE_SHA; %s selfHeal false; %s autoPromotionEnabled false\n' "$ARGO_APPS" "$KARGO_STAGE_ACCOUNTS"
  [ "$APPLY" = 1 ] || return 0
  bash "$THAW" || { echo "thaw.sh failed"; return 1; }
  set -a; eval "$(< "$ENV_FILE")"; set +a
  if [ -z "${FREEZE_SHA:-}" ] || [ -z "${THAW_PR:-}" ]; then echo "thaw.sh did not append FREEZE_SHA and THAW_PR"; return 1; fi
  until_ok 400 synced_to "$FREEZE_SHA" || { echo "tb-stage-app-of-apps did not sync a revision containing $FREEZE_SHA"; return 1; }
  until_ok 400 self_heal false || { echo "selfHeal is not false on every app in ARGO_APPS"; return 1; }
  until_ok 400 auto_promote false || { echo "auto-promotion for $KARGO_STAGE_ACCOUNTS is not false"; return 1; }
  open=$(open_promotions) || return 1
  [ -z "$open" ] || { echo "open $KARGO_STAGE_ACCOUNTS Promotion(s): $open"; return 1; }
  echo "four apps selfHeal false, auto-promotion false, no open Promotion"
}

pods() { "${KS[@]}" get pods -n thundermail -l app=stalwart -o name; "${KS[@]}" get pods -n accounts -o name; "${KS[@]}" get pods -n keycloak-customer -o name; }
none_left() { [ -z "$(pods)" ]; }
replicas_zero() {
  "${KS[@]}" get deploy,sts -n thundermail -o jsonpath='{range .items[*]}{.metadata.name} {.spec.replicas}{"\n"}{end}' | awk '$1 == "stalwart" && $2 != 0 {bad = 1} END {exit bad}' &&
    "${KS[@]}" get deploy -n accounts -o jsonpath='{range .items[*]}{.spec.replicas}{"\n"}{end}' | awk '$1 != 0 {bad = 1} END {exit bad}' &&
    "${KS[@]}" get sts -n keycloak-customer -o jsonpath='{range .items[*]}{.spec.replicas}{"\n"}{end}' | awk '$1 != 0 {bad = 1} END {exit bad}'
}

scale_down() {
  local before
  before=$(pods | grep -c . || true)
  run "${KS[@]}" -n thundermail scale deploy/stalwart --replicas=0 || return 1
  run "${KS[@]}" -n accounts scale deploy/accounts-web deploy/accounts-celery deploy/accounts-flower --replicas=0 || return 1
  run "${KS[@]}" -n keycloak-customer scale sts/keycloak-customer --replicas=0 || return 1
  printf '  + wait 300s for 0 pods, then 300s more and check every replica count is still 0\n'
  VALUE=$before
  [ "$APPLY" = 1 ] || { echo "pods now: $before"; return 0; }
  until_ok 300 none_left || { echo "pods still running after 300s: $(pods | tr '\n' ' ')"; return 1; }
  sleep 300
  replicas_zero || { echo "a workload scaled back up within 5 minutes"; return 1; }
  echo "pods_terminated=$before, all replicas still 0 after 5 minutes"
}

prereqs
helpers snapshot-gitops.sh restore-replicas.sh "$THAW"
step decisions "" decisions
step snap-gitops workloads snap
go_gate
step freeze "" freeze
step scale-down pods_terminated scale_down
finish
