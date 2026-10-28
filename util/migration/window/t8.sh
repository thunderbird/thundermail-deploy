#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need ACCOUNTS_LEGACY_PG_USER ACCOUNTS_LEGACY_PG_PASSWORD ACD_DIR KUBECONFIG_STAGE KUBECONFIG_HUB SECRET_DIR
pending T8_PR T8_REVERT_PR
REPO=thunderbird/accounts-deploy
SECRET_ID=mzla/tb-stage/accounts-db
NS=accounts
APP=tb-stage-accounts
JSON=$SECRET_DIR/accounts-db.json
mkdir -p "$SECRET_DIR"

start() {
  T8_START=$(date -u +%FT%TZ)
  setenv T8_START "$T8_START"
}

snap() {
  local out
  out=$(bash "$MIG_DIR/snapshot-secret-versions.sh" "$SECRET_ID") || return 1
  printf '%s\n' "$out"
  VALUE=$(grep -c . "$(tail -1 <<<"$out")")
  if restore_staged; then echo "restore already staged for this cut: $RESTORE"; return 0; fi
  printf '%s\n' \
    "gh pr ready $T8_REVERT_PR -R $REPO, an approving review, gh pr merge $T8_REVERT_PR -R $REPO --merge, then main -> env/tb-stage in $ACD_DIR (merge commit) and wait on the $APP sync revision" \
    "$(if unchanged; then echo "secret unchanged by this step: $SECRET_ID stays on version $(cut -f2 "$(tail -1 <<<"$out")"); no secret rollback, the revert PR is the restore. If a put did happen: "; fi)bash $MIG_DIR/restore-secret-version.sh $SECRET_ID $(cut -f2 "$(tail -1 <<<"$out")") --dry-run, then --apply, then force-sync externalsecret $NS/accounts-db" > "$RESTORE" || return 1
  mark_restore
}

gate() {
  printf '  + bash %s\n' "$MIG_DIR/gate-django-migrations.sh"
  [ "$APPLY" = 1 ] || return 0
  bash "$MIG_DIR/gate-django-migrations.sh"
}

legacy_sha() { jq -cnS --arg u "$ACCOUNTS_LEGACY_PG_USER" --arg p "$ACCOUNTS_LEGACY_PG_PASSWORD" '{username: $u, password: $p}' | sha256sum | cut -c1-16; }
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
  jq -n --arg u "$ACCOUNTS_LEGACY_PG_USER" --arg p "$ACCOUNTS_LEGACY_PG_PASSWORD" '{username: $u, password: $p}' > "$JSON" || return 1
  chmod 600 "$JSON"
  bash "$MIG_DIR/put-secret-version.sh" "$SECRET_ID" "$JSON" --apply
}

refreshed() { [ "$("${KS[@]}" -n "$NS" get externalsecret accounts-db -o jsonpath='{.status.refreshTime}')" \> "$T8_START" ]; }

refresh() {
  run "${KS[@]}" -n "$NS" annotate externalsecret accounts-db "force-sync=$(date +%s)" --overwrite || return 1
  printf '  + wait 300s for externalsecret accounts-db refreshTime > T8_START\n'
  [ "$APPLY" = 1 ] || return 0
  until_ok 300 refreshed || { echo "accounts-db did not refresh after T8_START"; return 1; }
}

app_rev() { [ "$("${KH[@]}" -n argocd get application "$APP" -o jsonpath='{.status.sync.revision}')" = "$T8_ENV_SHA" ]; }

env_sync() {
  merge_pr "$REPO" "$T8_PR" || return 1
  run gh pr edit "$T8_REVERT_PR" -R "$REPO" --base main || return 1
  run git -C "$ACD_DIR" fetch origin || return 1
  run git -C "$ACD_DIR" checkout -B env/tb-stage origin/env/tb-stage || return 1
  run git -C "$ACD_DIR" merge --no-ff --no-edit origin/main || { echo "main -> env/tb-stage did not merge cleanly in $ACD_DIR"; return 1; }
  run git -C "$ACD_DIR" push origin env/tb-stage || return 1
  printf '  + append T8_ENV_SHA, wait 400s for the %s sync revision\n' "$APP"
  [ "$APPLY" = 1 ] || return 0
  T8_ENV_SHA=$(git -C "$ACD_DIR" rev-parse HEAD)
  setenv T8_ENV_SHA "$T8_ENV_SHA" || return 1
  until_ok 400 app_rev || { echo "$APP did not sync $T8_ENV_SHA"; return 1; }
}

one() { [ "$("${KS[@]}" -n "$NS" get deploy accounts-web -o jsonpath='{.spec.replicas}')" = 1 ]; }

repoint() {
  local pending plan
  printf '  + wait 120s for deploy/accounts-web replicas 1, rollout status 600s for web, celery, flower, then showmigrations --plan unapplied = 0\n'
  [ "$APPLY" = 1 ] || return 0
  until_ok 120 one || { echo "deploy/accounts-web is not at 1 replica"; return 1; }
  "${KS[@]}" -n "$NS" rollout status deploy/accounts-web deploy/accounts-celery deploy/accounts-flower --timeout=600s || return 1
  plan=$("${KS[@]}" -n "$NS" exec deploy/accounts-web -- ./manage.py showmigrations --plan) || { echo "showmigrations failed"; return 1; }
  pending=$(grep -c '\[ \]' <<<"$plan" || true)
  echo "unapplied migrations: $pending; sign in at https://accounts.stage-thundermail.com and record the first-signin row"
  [ "$pending" = 0 ]
}

if [ "$(gh pr view "$T8_PR" -R "$REPO" --json state -q .state 2>/dev/null)" = MERGED ]; then
  echo "T8_PR is merged: resuming after the merge, check-prereqs.sh skipped (its PR pair row expects the PR open)"
else
  prereqs
fi
helpers snapshot-secret-versions.sh gate-django-migrations.sh put-secret-version.sh restore-secret-version.sh
step t8-start "" start
step snap-secret-versions secrets snap
step gate-django "" gate
step put-secret state put
step secret-refresh "" refresh
go_gate
step env-sync-accounts "" env_sync
step repoint-accounts "" repoint
finish
