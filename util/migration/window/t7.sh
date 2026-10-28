#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KC_LEGACY_PG_USER KC_LEGACY_PG_PASSWORD KCD_DIR KUBECONFIG_STAGE KUBECONFIG_HUB SECRET_DIR
pending T7_PR T7_REVERT_PR
REPO=thunderbird/keycloak-customer-deploy
SECRET_ID=mzla/tb-stage/keycloak-customer-db
NS=keycloak-customer
APP=tb-stage-keycloak-customer
ISSUER_HOST=auth.stage-thundermail.com
JSON=$SECRET_DIR/keycloak-customer-db.json
mkdir -p "$SECRET_DIR"

start() {
  T7_START=$(date -u +%FT%TZ)
  setenv T7_START "$T7_START"
}

snap() {
  local out realm
  realm=$(find "$SECRET_DIR" -maxdepth 1 -name 'realm-before-*.json' -size +0 -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)
  [ -n "$realm" ] || { echo "no realm-before snapshot in $SECRET_DIR (#1551)"; [ "$APPLY" = 0 ] || return 1; NEEDS=$((NEEDS + 1)); }
  out=$(bash "$MIG_DIR/snapshot-secret-versions.sh" "$SECRET_ID") || return 1
  printf '%s\n' "$out"
  VALUE=$(grep -c . "$(tail -1 <<<"$out")")
  if restore_staged; then echo "restore already staged for this cut: $RESTORE"; return 0; fi
  printf '%s\n' \
    "gh pr ready $T7_REVERT_PR -R $REPO, an approving review, gh pr merge $T7_REVERT_PR -R $REPO --merge, then main -> env/tb-stage in $KCD_DIR (merge commit) and wait on the $APP sync revision" \
    "$(if unchanged; then echo "secret unchanged by this step: $SECRET_ID stays on version $(cut -f2 "$(tail -1 <<<"$out")"); no secret rollback, the revert PR is the restore. If a put did happen: "; fi)bash $MIG_DIR/restore-secret-version.sh $SECRET_ID $(cut -f2 "$(tail -1 <<<"$out")") --dry-run, then --apply, then force-sync externalsecret $NS/keycloak-customer-db" \
    "before legacy Keycloak restarts on main: bash $MIG_DIR/restore-realm.sh with ${realm:-the realm-before snapshot}" > "$RESTORE" || return 1
  mark_restore
}

gate() {
  printf '  + bash %s\n' "$MIG_DIR/gate-keycloak-version.sh"
  [ "$APPLY" = 1 ] || return 0
  bash "$MIG_DIR/gate-keycloak-version.sh"
}

legacy_sha() { jq -cnS --arg u "$KC_LEGACY_PG_USER" --arg p "$KC_LEGACY_PG_PASSWORD" '{username: $u, password: $p}' | sha256sum | cut -c1-16; }
current_sha() { aws_stage secretsmanager get-secret-value --secret-id "$SECRET_ID" --output json | jq -r '.SecretString' | jq -cS . | sha256sum | cut -c1-16; }
unchanged() { [ "$(current_sha)" = "$(legacy_sha)" ]; }

put() {
  if unchanged; then
    VALUE=unchanged
    echo "unchanged: $SECRET_ID already holds the legacy credential; no put, the current version stays the rollback target"
    return 0
  fi
  VALUE=put
  if [ "$APPLY" = 0 ]; then printf '  + bash %s %s %s --apply (legacy credential JSON written only under --apply)\n' "$MIG_DIR/put-secret-version.sh" "$SECRET_ID" "$JSON"; return 0; fi
  jq -n --arg u "$KC_LEGACY_PG_USER" --arg p "$KC_LEGACY_PG_PASSWORD" '{username: $u, password: $p}' > "$JSON" || return 1
  chmod 600 "$JSON"
  bash "$MIG_DIR/put-secret-version.sh" "$SECRET_ID" "$JSON" --apply
}

refreshed() { [ "$("${KS[@]}" -n "$NS" get externalsecret keycloak-customer-db -o jsonpath='{.status.refreshTime}')" \> "$T7_START" ]; }

refresh() {
  run "${KS[@]}" -n "$NS" annotate externalsecret keycloak-customer-db "force-sync=$(date +%s)" --overwrite || return 1
  printf '  + wait 300s for externalsecret keycloak-customer-db refreshTime > T7_START\n'
  [ "$APPLY" = 1 ] || return 0
  until_ok 300 refreshed || { echo "keycloak-customer-db did not refresh after T7_START"; return 1; }
}

app_rev() { [ "$("${KH[@]}" -n argocd get application "$APP" -o jsonpath='{.status.sync.revision}')" = "$T7_ENV_SHA" ]; }

env_sync() {
  merge_pr "$REPO" "$T7_PR" || return 1
  run gh pr edit "$T7_REVERT_PR" -R "$REPO" --base main || return 1
  run git -C "$KCD_DIR" fetch origin || return 1
  run git -C "$KCD_DIR" checkout -B env/tb-stage origin/env/tb-stage || return 1
  run git -C "$KCD_DIR" merge --no-ff --no-edit origin/main || { echo "main -> env/tb-stage did not merge cleanly in $KCD_DIR"; return 1; }
  run git -C "$KCD_DIR" push origin env/tb-stage || return 1
  printf '  + append T7_ENV_SHA, wait 400s for the %s sync revision\n' "$APP"
  [ "$APPLY" = 1 ] || return 0
  T7_ENV_SHA=$(git -C "$KCD_DIR" rev-parse HEAD)
  setenv T7_ENV_SHA "$T7_ENV_SHA" || return 1
  until_ok 400 app_rev || { echo "$APP did not sync $T7_ENV_SHA"; return 1; }
}

two() { [ "$("${KS[@]}" -n "$NS" get sts keycloak-customer -o jsonpath='{.spec.replicas}')" = 2 ]; }

repoint() {
  local ip issuer
  printf '  + wait 120s for sts/keycloak-customer replicas 2, rollout status 600s, issuer check on %s\n' "$ISSUER_HOST"
  [ "$APPLY" = 1 ] || return 0
  until_ok 120 two || { echo "sts/keycloak-customer is not at 2 replicas"; return 1; }
  "${KS[@]}" -n "$NS" rollout status sts/keycloak-customer --timeout=600s || return 1
  ip=$(doh "$ISSUER_HOST")
  issuer=$(curl -s -m 20 --resolve "$ISSUER_HOST:443:${ip:-0.0.0.0}" "https://$ISSUER_HOST/realms/tbpro/.well-known/openid-configuration" | jq -r '.issuer // empty')
  echo "issuer: ${issuer:-none}"
  [ "$issuer" = "https://$ISSUER_HOST/realms/tbpro" ]
}

if [ "$(gh pr view "$T7_PR" -R "$REPO" --json state -q .state 2>/dev/null)" = MERGED ]; then
  echo "T7_PR is merged: resuming after the merge, check-prereqs.sh skipped (its PR pair row expects the PR open)"
else
  prereqs
fi
helpers snapshot-secret-versions.sh gate-keycloak-version.sh put-secret-version.sh restore-secret-version.sh restore-realm.sh
step t7-start "" start
step snap-secret-versions secrets snap
step gate-keycloak "" gate
step put-secret state put
step secret-refresh "" refresh
go_gate
step env-sync-keycloak "" env_sync
step repoint-keycloak "" repoint
finish
