#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need OLD_STALWART_URL OLD_STALWART_USER OLD_STALWART_PASSWORD STALWART_URL STALWART_USER STALWART_PASSWORD \
  STALWART_LEGACY_PG_HOST STALWART_LEGACY_PG_USER STALWART_LEGACY_PG_DB STALWART_TARGET_PG_HOST STALWART_TARGET_PG_USER STALWART_TARGET_PG_DB \
  TARGET_BLOB_BUCKET REDIS_URL KUBECONFIG_STAGE SECRET_DIR TIMING_TSV
mkdir -p "$SECRET_DIR"
PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
S=$SECRET_DIR
NS=(-n thundermail)

printf '%s\n' \
  "$(shq "${KS[@]}" "${NS[@]}" scale deploy/stalwart --replicas=0)" \
  "bash $MIG_DIR/restore-target-db.sh /work/target-before.dump --dry-run" \
  "bash $MIG_DIR/restore-target-db.sh /work/target-before.dump --apply" \
  "$(shq "${KS[@]}" "${NS[@]}" set env deploy/stalwart STALWART_RECOVERY_MODE-)" \
  "retry instead (DROP SCHEMA and copy again): bash $0 --apply --go '<operator confirmation from Decisions and forks>'" > "$RESTORE"

replicas() { "${KS[@]}" "${NS[@]}" get deploy/stalwart -o jsonpath='{.spec.replicas}'; }
runner_up() { [ "$("${KS[@]}" "${NS[@]}" get pod runner -o jsonpath='{.status.phase}' 2>/dev/null)" = Running ]; }
stalwart_up() { [ "$("${KS[@]}" "${NS[@]}" get pod -l app=stalwart -o jsonpath='{.items[0].status.phase}' 2>/dev/null)" = Running ]; }
schema_ok() { [ "$(bash "$MIG_DIR/schema-version.sh" 2>/dev/null)" = 00000006 ]; }
blob_ok() { awk -F'\t' '$1 == "blob-check" && $4 == "differences" {v = $5} END {exit !(v == "0")}' "$TIMING_TSV"; }

preflight() {
  local r
  until_ok 60 runner_up || { echo "runner pod is not Running: re-apply util/migration/runner-pod.yaml"; return 1; }
  echo "runner Running"
  r=$(replicas) || return 1
  echo "deploy/stalwart replicas=$r"
  if [ "$APPLY" = 0 ]; then
    [ "$r" = 0 ] || echo "deploy/stalwart must be at 0 replicas before the DROP in-window"
    printf '  + bash %s --assert-only\n  + pod sh -c %s\n' "$MIG_DIR/restore-target-db.sh" "'find /work/target-before.dump -mmin -720'"
    return 0
  fi
  [ "$r" = 0 ] || { echo "deploy/stalwart must be at 0 replicas before the DROP"; return 1; }
  bash "$MIG_DIR/restore-target-db.sh" --assert-only || return 1
  if [ -z "$(pod sh -c 'test -s /work/target-before.dump && find /work/target-before.dump -mmin -720')" ]; then
    echo "target-before.dump missing or older than 12 h: re-taking it"
    run pod pg_dump -Fc --no-owner -h "$STALWART_TARGET_PG_HOST" -U "$STALWART_TARGET_PG_USER" -d "$STALWART_TARGET_PG_DB" -f /work/target-before.dump || return 1
  fi
  pod sh -c 'cd /work && sha256sum target-before.dump' >> "$EVIDENCE_DIR/dumps.sha256" || return 1
  tail -1 "$EVIDENCE_DIR/dumps.sha256"
  bash "$MIG_DIR/restore-target-db.sh" /work/target-before.dump --dry-run | tail -1
}

queue_drain() {
  local body
  body=$(curl -sk -m 15 -K <(curl_user "$OLD_STALWART_USER" "$OLD_STALWART_PASSWORD") "$OLD_STALWART_URL/api/queue/messages?limit=1") || { echo "legacy queue API did not answer"; return 1; }
  VALUE=$(jq -r '.data.total // empty' <<<"$body" 2>/dev/null || true)
  echo "legacy queue total=${VALUE:-unreadable}"
  [ -n "$VALUE" ] || return 1
  [ "$APPLY" = 0 ] || [ "$VALUE" = 0 ] || { echo "queue not drained: wait or flush, record it, then rerun"; return 1; }
}

dump() {
  run "$PY" "$MIG_DIR/migrate_v016.py" dump --url "$OLD_STALWART_URL" --username "$OLD_STALWART_USER" --password "$OLD_STALWART_PASSWORD" --settings "$S/settings.json" --principals "$S/principals.json" || return 1
  [ "$APPLY" = 0 ] || { [ -s "$S/settings.json" ] && [ -s "$S/principals.json" ]; }
}

convert() {
  local rc=0
  run_err "$S/convert.stderr" "$PY" "$MIG_DIR/migrate_v016.py" convert --settings "$S/settings.json" --principals "$S/principals.json" --config "$S/config.json" --output "$S/export.json" --unmigrated-output "$S/unmigrated.txt" || rc=$?
  [ "$APPLY" = 1 ] || return 0
  [ "$rc" != 1 ] || { echo "convert exit 1 (#1166 fork): apply the pre-recorded remediation, then rerun"; return 1; }
  [ "$rc" = 0 ] || { echo "convert exit $rc"; return 1; }
  [ -s "$S/export.json" ] || { echo "convert wrote no export.json"; return 1; }
  VALUE=$(grep -c . "$S/export.json")
  DROPPED=$(grep -ciE 'dropp|renam|skipp' "$S/convert.stderr" || true)
  echo "export_lines=$VALUE dropped_accounts=$DROPPED"
}

retarget() {
  local host
  run bash "$MIG_DIR/retarget-export.sh" "$S/export.json" || return 1
  [ "$APPLY" = 1 ] || return 0
  host=$(sed -E 's|^[a-z]+://([^@/]*@)?([^:/]+).*|\2|' <<<"$REDIS_URL")
  [ "$(jq -r 'select(.object == "BlobStore") | .value.bucket' "$S/export.json")" = "$TARGET_BLOB_BUCKET" ] || { echo "export BlobStore is not $TARGET_BLOB_BUCKET"; return 1; }
  grep -qF "$host" <<<"$(jq -r 'select(.object == "InMemoryStore") | .value.url' "$S/export.json")" || { echo "export InMemoryStore does not point at the REDIS_URL host"; return 1; }
  echo "export BlobStore=$TARGET_BLOB_BUCKET InMemoryStore host=$host"
}

dump_legacy() {
  run pod pg_dump -Fc --no-owner -h "$STALWART_LEGACY_PG_HOST" -U "$STALWART_LEGACY_PG_USER" -d "$STALWART_LEGACY_PG_DB" -f /work/legacy.dump || return 1
  [ "$APPLY" = 1 ] || return 0
  VALUE=$(pod stat -c %s /work/legacy.dump) || return 1
  pod sh -c 'cd /work && sha256sum legacy.dump' >> "$EVIDENCE_DIR/dumps.sha256" || return 1
  tail -1 "$EVIDENCE_DIR/dumps.sha256"
  [ "$VALUE" -gt 0 ]
}

restore() {
  local out rc n script
  script="pg_restore -j4 --no-owner --no-privileges -h \"\$1\" -U \"\$2\" -d \"\$3\" /work/legacy.dump 2>/work/restore.err; rc=\$?; echo \"pg_restore_exit=\$rc other_errors=\$(grep -v 'schema \"public\" already exists' /work/restore.err | grep -c .)\""
  [ "$APPLY" = 0 ] || { bash "$MIG_DIR/restore-target-db.sh" --assert-only && [ "$(replicas)" = 0 ]; } || { echo "assertions failed right before the DROP"; return 1; }
  run pod psql -h "$STALWART_TARGET_PG_HOST" -U "$STALWART_TARGET_PG_USER" -d "$STALWART_TARGET_PG_DB" -v ON_ERROR_STOP=1 -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' || return 1
  out=$(run pod sh -c "$script" sh "$STALWART_TARGET_PG_HOST" "$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_DB") || return 1
  printf '%s\n' "$out"
  [ "$APPLY" = 1 ] || return 0
  out=$(tail -1 <<<"$out")
  rc=$(sed -n 's/^pg_restore_exit=\([0-9]*\) .*/\1/p' <<<"$out")
  n=$(sed -n 's/.* other_errors=\([0-9]*\)$/\1/p' <<<"$out")
  [ "$rc" = 0 ] || [ "$n" = 0 ]
}

recovery_boot() {
  run "${KS[@]}" "${NS[@]}" set env deploy/stalwart STALWART_RECOVERY_MODE=true || return 1
  run "${KS[@]}" "${NS[@]}" scale deploy/stalwart --replicas=1 || return 1
  printf '  + wait 120s for the stalwart pod Running, then 900s for schema-version.sh = 00000006\n'
  [ "$APPLY" = 1 ] || return 0
  until_ok 120 stalwart_up || { echo "stalwart pod not Running after 120s"; return 1; }
  until_ok 900 schema_ok || { echo "schema version is $(bash "$MIG_DIR/schema-version.sh" 2>/dev/null || echo unreadable), not 00000006, after 900s"; return 1; }
  VALUE=00000006
}

apply_export() {
  if [ "$APPLY" = 1 ]; then port_forward || return 1; fi
  run_to "$S/apply.log" stalwart-cli apply --file "$S/export.json" || { echo "stalwart-cli apply failed; see $S/apply.log"; return 1; }
  [ "$APPLY" = 1 ] || return 0
  VALUE=$(grep -c '✗' "$S/apply.log" || true)
  echo "apply.log lines=$(grep -c . "$S/apply.log") errors=$VALUE"
  [ "$VALUE" = 0 ]
}

admin_id() {
  local id host
  printf '  + curl -s -K <(user STALWART_USER:STALWART_PASSWORD) %s/jmap/session | jq -re %s\n' "$STALWART_URL" "'.primaryAccounts[\"urn:stalwart:jmap\"]'"
  [ "$APPLY" = 1 ] || { setenv STALWART_ADMIN_ACCOUNT_ID '<from the session>'; return 0; }
  id=$(curl -s -m 15 -K <(curl_user "$STALWART_USER" "$STALWART_PASSWORD") "$STALWART_URL/jmap/session" | jq -re '.primaryAccounts["urn:stalwart:jmap"]') || { echo "no urn:stalwart:jmap account in the session"; return 1; }
  [ -n "$id" ] && [ "$id" != null ] || return 1
  setenv STALWART_ADMIN_ACCOUNT_ID "$id"
  host=$(sed -E 's|^[a-z]+://([^@/]*@)?([^:/]+).*|\2|' <<<"$REDIS_URL")
  grep -qF "$TARGET_BLOB_BUCKET" <<<"$(stalwart-cli query BlobStore)" || { echo "BlobStore read back is not $TARGET_BLOB_BUCKET"; return 1; }
  grep -qF "$host" <<<"$(stalwart-cli query InMemoryStore)" || { echo "InMemoryStore read back is not the REDIS_URL host"; return 1; }
  echo "read back: BlobStore=$TARGET_BLOB_BUCKET InMemoryStore host=$host"
}

leave_recovery() {
  if [ "$APPLY" = 0 ]; then
    if blob_ok; then echo "blob-check differences=0 is in $TIMING_TSV"; else echo "no blob-check differences=0 row in $TIMING_TSV yet (W3 writes it)"; fi
  else
    until_ok 3600 blob_ok || { echo "no blob-check differences=0 row after 3600s; stay in recovery mode"; return 1; }
  fi
  run "${KS[@]}" "${NS[@]}" set env deploy/stalwart STALWART_RECOVERY_MODE- || return 1
  run "${KS[@]}" "${NS[@]}" rollout status deploy/stalwart --timeout=600s
}

prereqs
helpers migrate_v016.py retarget-export.sh schema-version.sh restore-target-db.sh stalwart-cli "$PY"
if [ "$APPLY" = 1 ] && grep -q $'^db-restore\t' "$TIMING_TSV" 2>/dev/null; then
  [ -n "$GO" ] || stop "db-restore already ran once: a second DROP SCHEMA needs the operator's confirmation (--go)"
  go_gate
fi
step w4-preflight "" preflight
step queue-drain queued queue_drain
step dump "" dump
step convert export_lines convert
[ "$APPLY" = 0 ] || again dropped_accounts "$DROPPED"
[ "$APPLY" = 0 ] || [ "$DROPPED" = 0 ] || stop "convert.stderr reports $DROPPED dropped, renamed or skipped account line(s)"
step retarget "" retarget
step db-dump-legacy bytes dump_legacy
step db-restore "" restore
step recovery-boot schema recovery_boot
step apply errors apply_export
step admin-account-id "" admin_id
step leave-recovery "" leave_recovery
finish
