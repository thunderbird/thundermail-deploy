#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need ARGO_APPS KARGO_PROJECT_ACCOUNTS KARGO_STAGE_ACCOUNTS KUBECONFIG_STAGE KUBECONFIG_HUB PI_DIR
pending THAW_PR REPLICAS_BEFORE
PI=thunderbird/platform-infrastructure
ACC=thunderbird/thunderbird-accounts
PROMOTION=$EVIDENCE_DIR/promotion-accounts-$TS.yaml

snap() {
  local out
  out=$(bash "$MIG_DIR/snapshot-gitops.sh" post) || return 1
  printf '%s\n' "$out"
  VALUE=$(grep -c . "$(tail -1 <<<"$out")")
  if restore_staged; then echo "restore already staged for this cut: $RESTORE"; return 0; fi
  printf '%s\n' "revert the thaw merge commit: git revert -m 1 \"\$THAW_SHA\" on a branch from PI main, new PR, merge commit" > "$RESTORE" || return 1
  mark_restore
}

recovery_off() {
  local v
  v=$("${KS[@]}" -n thundermail get deploy stalwart -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="STALWART_RECOVERY_MODE")]}') || return 1
  [ -z "$v" ] || { echo "STALWART_RECOVERY_MODE is still set on deploy/stalwart"; return 1; }
  echo "recovery mode off"
}

workloads_up() {
  [ "$("${KS[@]}" -n accounts get deploy accounts-web -o jsonpath='{.spec.replicas}')" = 1 ] &&
    [ "$("${KS[@]}" -n keycloak-customer get sts keycloak-customer -o jsonpath='{.spec.replicas}')" = 2 ]
}

scale_up() {
  local r
  r=$("${KS[@]}" -n thundermail get deploy stalwart -o jsonpath='{.spec.replicas}') || return 1
  echo "deploy/stalwart replicas=$r"
  if [ "$r" = 0 ]; then
    run "${KS[@]}" -n thundermail scale deploy/stalwart --replicas=1 || return 1
    run "${KS[@]}" -n thundermail rollout status deploy/stalwart --timeout=600s || return 1
  fi
  printf '  + wait 60s for accounts-web 1 and keycloak-customer 2, else bash %s %s --apply\n' "$MIG_DIR/restore-replicas.sh" "$REPLICAS_BEFORE"
  [ "$APPLY" = 1 ] || return 0
  case "$REPLICAS_BEFORE" in */replicas-before-thaw-*) echo "REPLICAS_BEFORE names the post-window snapshot"; return 1 ;; esac
  until_ok 60 workloads_up || bash "$MIG_DIR/restore-replicas.sh" "$REPLICAS_BEFORE" --apply || return 1
  workloads_up || { echo "accounts-web or keycloak-customer is still not scaled up"; return 1; }
  "${KS[@]}" -n accounts rollout status deploy/accounts-web --timeout=300s || return 1
  "${KS[@]}" -n keycloak-customer rollout status sts/keycloak-customer --timeout=300s
}

cleanup() {
  local eps
  run "${KS[@]}" -n thundermail delete -f "$MIG_DIR/runner-pod.yaml" --ignore-not-found || return 1
  run "${KS[@]}" -n thundermail delete secret rehearsal-legacy-pg --ignore-not-found || return 1
  eps=$("${KS[@]}" -n thundermail get endpointslices -l kubernetes.io/service-name=stalwart-mail -o jsonpath='{range .items[*].endpoints[*]}{.targetRef.name}:{.conditions.ready} {end}') || return 1
  echo "stalwart-mail endpoints: ${eps:-none}"
  ! tr ' ' '\n' <<<"$eps" | grep -v '^$' | grep -qv '^stalwart-' || { echo "an endpoint other than stalwart-* is in stalwart-mail"; return 1; }
}

freight_race() {
  local cur dev stage
  cur=$("${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get stage "$KARGO_STAGE_ACCOUNTS" -o jsonpath='{.status.freightHistory[0].items.*.name}') || return 1
  [ -n "$cur" ] || { echo "no Freight on $KARGO_STAGE_ACCOUNTS"; return 1; }
  setenv CURRENT_ACCOUNTS_FREIGHT "$cur" || return 1
  CURRENT_ACCOUNTS_FREIGHT=$cur
  "${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get freight --sort-by=.metadata.creationTimestamp -o name | tail -3
  dev=$("${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get stage tb-dev -o jsonpath='{.status.freightHistory[0].items.*.name}' 2>/dev/null || true)
  stage=$cur
  echo "tb-dev=${dev:-none} $KARGO_STAGE_ACCOUNTS=$stage"
  [ -z "$dev" ] || [ "$dev" = "$stage" ] || echo "tb-dev is ahead: check the Keycloak image version against migration_model (#1548) and record the decision before the thaw go"
}

app_rev() { "${KH[@]}" -n argocd get application tb-stage-app-of-apps -o jsonpath='{.status.sync.revision}'; }
synced_to() { local r; r=$(app_rev) && [ -n "$r" ] && git -C "$PI_DIR" fetch -q origin main && git -C "$PI_DIR" merge-base --is-ancestor "$1" "$r"; }
self_heal() { local a; for a in $ARGO_APPS; do [ "$("${KH[@]}" -n argocd get application "$a" -o jsonpath='{.spec.syncPolicy.automated.selfHeal}')" = "$1" ] || return 1; done; }
auto_promote() { [ "$("${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get projectconfig "$KARGO_PROJECT_ACCOUNTS" -o jsonpath="{.spec.promotionPolicies[?(@.stage==\"$KARGO_STAGE_ACCOUNTS\")].autoPromotionEnabled}")" = "$1" ]; }

thaw() {
  local sha
  merge_pr "$PI" "$THAW_PR" || return 1
  printf '  + append THAW_SHA; wait 400s each: app-of-apps sync revision contains THAW_SHA; %s selfHeal true; %s autoPromotionEnabled true\n' "$ARGO_APPS" "$KARGO_STAGE_ACCOUNTS"
  [ "$APPLY" = 1 ] || return 0
  sha=$(gh pr view "$THAW_PR" -R "$PI" --json mergeCommit -q '.mergeCommit.oid // empty') || return 1
  [ -n "$sha" ] || { echo "THAW_PR $THAW_PR has no merge commit"; return 1; }
  setenv THAW_SHA "$sha" || return 1
  until_ok 400 synced_to "$sha" || { echo "tb-stage-app-of-apps did not sync a revision containing $sha"; return 1; }
  until_ok 400 self_heal true || { echo "selfHeal is not true on every app in ARGO_APPS"; return 1; }
  until_ok 400 auto_promote true || { echo "auto-promotion for $KARGO_STAGE_ACCOUNTS is not true"; return 1; }
  local apps; read -ra apps <<<"$ARGO_APPS"
  "${KH[@]}" -n argocd get application "${apps[@]}" -o jsonpath='{range .items[*]}{.metadata.name} {.spec.syncPolicy.automated.selfHeal} {.status.sync.status} {.status.health.status}{"\n"}{end}'
}

wf_state() { gh api "repos/$ACC/actions/workflows/merge.yml" --jq .state; }

accounts_ci_off() {
  local st who
  printf '  + gh workflow disable merge.yml -R %s (deploy-stage puts the legacy ECS desired_count back to 1); row accounts-merge-yml: state after, who; re-enable only after the thunderbird-accounts desired_count 0 PR merges\n' "$ACC"
  st=$(wf_state) || return 1
  VALUE=$st
  echo "merge.yml state now: $st"
  [ "$APPLY" = 1 ] || return 0
  [ "$st" = disabled_manually ] || gh workflow disable merge.yml -R "$ACC" || return 1
  who=$(gh api user --jq .login) || return 1
  VALUE=$(wf_state) || return 1
  mark accounts-merge-yml state "$VALUE by $who"
  [ "$VALUE" = disabled_manually ]
}

accounts_ci_verify() {
  VALUE=$(wf_state) || return 1
  if [ "$VALUE" != disabled_manually ]; then
    [ "$APPLY" = 1 ] || { echo "merge.yml is $VALUE before the apply; the apply expects disabled_manually"; return 0; }
    echo "$ACC merge.yml is $VALUE, want disabled_manually"; return 1
  fi
  echo "$ACC merge.yml disabled_manually; gh workflow enable merge.yml -R $ACC only after the desired_count 0 PR merges"
}

last_promo() { "${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get stage "$KARGO_STAGE_ACCOUNTS" -o jsonpath='{.status.lastPromotion.name}'; }
promoted() { [ "$(last_promo)" != "$PROMO_BEFORE" ]; }
stage_get() { "${KH[@]}" -n "$KARGO_PROJECT_ACCOUNTS" get stage "$KARGO_STAGE_ACCOUNTS" -o jsonpath="$1"; }
ver_id() { stage_get '{.status.freightHistory[0].verificationHistory[0].id}'; }
verified() { [ "$(ver_id)" != "$VER_BEFORE" ] && [ "$(stage_get '{.status.freightHistory[0].verificationHistory[0].phase}')" = Successful ]; }

kargo_verify() {
  printf '  + wait 120s for an auto-promotion; else create a Promotion from promotion-accounts.yaml.tpl with CURRENT_ACCOUNTS_FREIGHT; wait 1200s for a new lastPromotion and a newer Successful verification; effectiveAutoPromotionHolds empty\n'
  [ "$APPLY" = 1 ] || return 0
  local cur holds
  PROMO_BEFORE=$(last_promo) || return 1
  VER_BEFORE=$(ver_id) || return 1
  if ! until_ok 120 promoted; then
    cur=$(stage_get '{.status.freightHistory[0].items.*.name}') || return 1
    [ "$cur" = "$CURRENT_ACCOUNTS_FREIGHT" ] || { echo "$KARGO_STAGE_ACCOUNTS now holds $cur, not CURRENT_ACCOUNTS_FREIGHT $CURRENT_ACCOUNTS_FREIGHT: no manual re-promote"; return 1; }
    sed "s/__TS__/$(date -u +%s)/; s/__FREIGHT__/$CURRENT_ACCOUNTS_FREIGHT/" "$MIG_DIR/promotion-accounts.yaml.tpl" > "$PROMOTION" || return 1
    "${KH[@]}" create -f "$PROMOTION" || return 1
  fi
  until_ok 1200 promoted || { echo "no new Promotion on $KARGO_STAGE_ACCOUNTS"; return 1; }
  until_ok 1200 verified || { echo "no new Successful verification on $KARGO_STAGE_ACCOUNTS after 1200s"; return 1; }
  holds=$(stage_get '{.status.effectiveAutoPromotionHolds}')
  [ -z "$holds" ] || { echo "effectiveAutoPromotionHolds is not empty: $holds"; return 1; }
  echo "lastPromotion $(last_promo) verified ($(ver_id)); no auto-promotion holds"
}

prereqs
helpers snapshot-gitops.sh restore-replicas.sh promotion-accounts.yaml.tpl runner-pod.yaml
step snap-gitops-post workloads snap
step recovery-off "" recovery_off
step scale-up "" scale_up
step cleanup "" cleanup
step freight-race "" freight_race
go_gate
step accounts-ci-off state accounts_ci_off
step thaw "" thaw
step kargo-verify "" kargo_verify
step accounts-ci-verify state accounts_ci_verify
printf 'after #1577 signs off: rm -rf %s %s\n' "$SECRET_DIR" "$ENV_FILE"
finish
