#!/usr/bin/env bash
set -euo pipefail
PHASE=""
A=()
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) [ $# -ge 2 ] || { echo "--phase needs read, reset or boot" >&2; exit 64; }; PHASE=$2; shift 2 ;;
    *) A+=("$1"); shift ;;
  esac
done
set -- "${A[@]+"${A[@]}"}"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
case "$PHASE" in
  ''|read|reset|boot) ;;
  *) die "--phase is read, reset or boot, not '$PHASE'" ;;
esac
[ -z "$PHASE" ] || [ "$TARGET_ENV" = tb-prod ] || die "W4 on $TARGET_ENV runs without --phase"
NEED_READ=(OLD_STALWART_URL OLD_STALWART_USER OLD_STALWART_PASSWORD TARGET_BLOB_BUCKET REDIS_URL SECRET_DIR TIMING_TSV)
NEED_RESET=(STALWART_URL STALWART_USER STALWART_PASSWORD STALWART_TARGET_PG_HOST STALWART_TARGET_PG_USER STALWART_TARGET_PG_DB TARGET_BLOB_BUCKET REDIS_URL KUBECONFIG_TARGET SECRET_DIR TIMING_TSV NEON_PROJECT_STALWART_LEGACY NEON_LEGACY_STALWART_BRANCH)
if [ "$TARGET_ENV" != tb-prod ]; then
  need OLD_STALWART_URL OLD_STALWART_USER OLD_STALWART_PASSWORD STALWART_URL STALWART_USER STALWART_PASSWORD \
    STALWART_LEGACY_PG_HOST STALWART_LEGACY_PG_USER STALWART_LEGACY_PG_DB STALWART_TARGET_PG_HOST STALWART_TARGET_PG_USER STALWART_TARGET_PG_DB \
    LEGACY_BLOB_BUCKET TARGET_BLOB_BUCKET REDIS_URL KUBECONFIG_TARGET SECRET_DIR TIMING_TSV
elif [ "$PHASE" = read ]; then
  need "${NEED_READ[@]}"
elif [ -n "$PHASE" ]; then
  need "${NEED_RESET[@]}"
else
  need "${NEED_READ[@]}" "${NEED_RESET[@]}"
fi
mkdir -p "$SECRET_DIR"
PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
S=$SECRET_DIR
NS=(-n thundermail)
POD=("${KS[@]}" "${NS[@]}" exec -i runner -c postgres --)
PROBE="if [ ! -s /work/target-before.dump ]; then echo missing; elif [ -n \"\$(find /work/target-before.dump -mmin -720)\" ]; then echo fresh; else echo stale; fi"
PRINCIPAL_WARN='WARN (failed to fetch|skipping principal type)'
TENANT_SKIP="WARN skipping principal type 'tenant': .*'Enterprise feature'"
DROP_RE='^warning: (dropping|renaming) (User|Group) '
DROP_BASELINE=$SECRET_DIR/convert-dropped-baseline.txt
RECOVERY_PR="thundermail-deploy PR ${TM_RECOVERY_PR:-<TM_RECOVERY_PR>}"
RECOVERY_WAIT=${RECOVERY_WAIT:-$([ "$TARGET_ENV" = tb-prod ] && echo 3600 || echo 1800)}
CUTOVER_BRANCH=tb-prod-cutover
SOURCE_BRANCH=tb-prod-cutover-source
DUMP_CMD=("$PY" "$MIG_DIR/migrate_v016.py" dump --url "${OLD_STALWART_URL:-}" --username "${OLD_STALWART_USER:-}" --password "${OLD_STALWART_PASSWORD:-}" --settings "$S/settings.json" --principals "$S/principals.json")
[ "$TARGET_ENV" != tb-prod ] || DUMP_CMD=("$PY" "$MIG_DIR/dump_legacy.py" --settings "$S/settings.json" --principals "$S/principals.json" --workers "${DUMP_WORKERS:-8}")
REVERT="revert the recovery-mode PR ($RECOVERY_PR) with a revert PR, merge it, and wait until Argo CD has synced deploy/stalwart to 0 replicas and no stalwart pod is left"

if [ "$TARGET_ENV" = tb-prod ]; then
  printf '%s\n' \
    "$REVERT: $(shq "${KS[@]}" "${NS[@]}" get deploy/stalwart -o jsonpath='{.spec.replicas}') prints 0 and $(shq "${KS[@]}" "${NS[@]}" get pods -l app=stalwart) lists no pod" \
    "resume instead when only the wait for the recovery-mode pod ran out (no second reset): bash $0 --phase boot --apply" \
    "retry the reset instead: $REVERT, then bash $0 --phase reset --apply --go '<operator confirmation from Decisions and forks>', and merge a new recovery-mode PR when it waits for the recovery-mode pod" > "$RESTORE"
else
  printf '%s\n' \
    "$REVERT: $(shq "${KS[@]}" "${NS[@]}" get deploy/stalwart -o jsonpath='{.spec.replicas}') prints 0 and $(shq "${KS[@]}" "${NS[@]}" get pods -l app=stalwart) lists no pod" \
    "bash $MIG_DIR/restore-target-db.sh /work/target-before.dump --dry-run" \
    "bash $MIG_DIR/restore-target-db.sh /work/target-before.dump --apply" \
    "retry instead (DROP SCHEMA and copy again): $REVERT, then bash $0 --apply --go '<operator confirmation from Decisions and forks>', and merge a new recovery-mode PR when it waits for the recovery-mode pod" > "$RESTORE"
fi

replicas() { "${KS[@]}" "${NS[@]}" get deploy/stalwart -o jsonpath='{.spec.replicas}'; }
runner_up() { [ "$("${KS[@]}" "${NS[@]}" get pod runner -o jsonpath='{.status.phase}' 2>/dev/null)" = Running ]; }
RECOVERY_OK="deploy replicas=1 ready=1 updated=1 total=1 observed=true recovery_env=true pods=1 ready=1 recovery=1"
recovery_state() {
  local d p
  d=$("${KS[@]}" "${NS[@]}" get deploy/stalwart -o json | jq -r '"deploy replicas=\(.spec.replicas // 0) ready=\(.status.readyReplicas // 0) updated=\(.status.updatedReplicas // 0) total=\(.status.replicas // 0) observed=\((.status.observedGeneration // 0) >= .metadata.generation) recovery_env=\([.spec.template.spec.containers[] | select(.name == "stalwart") | .env[]? | select(.name == "STALWART_RECOVERY_MODE") | .value] | join(",") | if . == "" then "unset" else . end)"') || { STATE="deploy/stalwart not readable"; return 1; }
  p=$("${KS[@]}" "${NS[@]}" get pods -l app=stalwart -o json | jq -r '(.items | length) as $all | [.items[] | select(any(.metadata.ownerReferences[]?; .kind == "ReplicaSet" and (.name | startswith("stalwart-"))))] | "pods=\($all) ready=\(map(select(.metadata.deletionTimestamp == null and any(.status.conditions[]?; .type == "Ready" and .status == "True"))) | length) recovery=\(map(select(any(.spec.containers[] | select(.name == "stalwart") | .env[]?; .name == "STALWART_RECOVERY_MODE" and .value == "true"))) | length)"') || { STATE="stalwart pods not readable"; return 1; }
  STATE="$d $p"
  [ "$STATE" = "$RECOVERY_OK" ]
}
schema_ok() { [ "$(bash "$MIG_DIR/schema-version.sh" 2>/dev/null)" = 00000006 ]; }
blob_ok() { awk -F'\t' '$1 == "cut-ecs" {c = NR} $1 == "snap-blobs" {s = NR; v = ""} $1 == "stop-W3" {v = ""} $1 == "blob-check" && $4 == "differences" {v = $5; b = NR} END {exit !(c > 0 && s > c && b > s && v == "0")}' "$TIMING_TSV"; }
show_to() { local f=$1; shift; printf '  + %s >> %s\n' "$(shq "$@")" "$f"; }
no_legacy_db() {
  legacy_stalwart_pg && return 1
  printf '%s%s reads the legacy Stalwart database: refused on %s, whose runner holds no credential for it (the cutover branch replaces this step)\n' "$([ "$APPLY" = 1 ] || echo 'NEED  ')" "$1" "$TARGET_ENV"
  [ "$APPLY" = 1 ] || NEEDS=$((NEEDS + 1))
}

retake() {
  [ -z "$(after_cut drop-schema)" ] || { echo "target-before.dump is $1 but DROP SCHEMA already ran for this cut: a new dump would replace the pre-migration restore point"; return 1; }
  run "${POD[@]}" pg_dump -Fc --no-owner -h "$STALWART_TARGET_PG_HOST" -U "$STALWART_TARGET_PG_USER" -d "$STALWART_TARGET_PG_DB" -f "/work/target-before-$TS.dump" || return 1
  run "${POD[@]}" mv "/work/target-before-$TS.dump" /work/target-before.dump
}

preflight() {
  local r state
  if [ "$APPLY" = 0 ] && ! runner_up; then
    printf 'NEED  runner pod not Running: apply util/migration/runner-pod.yaml before W4 (dry run continues)\n'
    NEEDS=$((NEEDS + 1))
  else
    until_ok 60 runner_up || { echo "runner pod is not Running: re-apply util/migration/runner-pod.yaml"; return 1; }
    echo "runner Running"
  fi
  if runner_up; then
    local who h u d err
    for who in STALWART_LEGACY STALWART_TARGET; do
      if [ "$who" = STALWART_LEGACY ] && ! legacy_stalwart_pg; then echo "psql select 1 on $who: skipped, the $TARGET_ENV runner holds no legacy Stalwart credential"; continue; fi
      h=${who}_PG_HOST u=${who}_PG_USER d=${who}_PG_DB
      if err=$("${POD[@]}" psql -h "${!h}" -U "${!u}" -d "${!d}" -Atc 'select 1' 2>&1 >/dev/null); then
        echo "psql select 1 on $who (${!h}): ok"
      else
        echo "psql select 1 on $who (${!h}) failed: $(tail -1 <<<"$err")"
        [ "$APPLY" = 0 ] || return 1
        NEEDS=$((NEEDS + 1))
      fi
    done
  fi
  r=$(replicas) || return 1
  echo "deploy/stalwart replicas=$r"
  if [ "$APPLY" = 0 ]; then
    [ "$r" = 0 ] || echo "deploy/stalwart must be at 0 replicas before the DROP in-window"
    printf '  + bash %s --assert-only\n' "$MIG_DIR/restore-target-db.sh"
    printf '  + %s\n' "$(shq "${POD[@]}" sh -c "$PROBE")"
    echo "  if missing or stale, and no drop-schema row exists for this cut:"
    retake missing || true
    show_to "$EVIDENCE_DIR/dumps.sha256" "${POD[@]}" sh -c 'cd /work && sha256sum target-before.dump'
    printf '  + bash %s /work/target-before.dump --dry-run\n' "$MIG_DIR/restore-target-db.sh"
    return 0
  fi
  [ "$r" = 0 ] || { echo "deploy/stalwart must be at 0 replicas before the DROP: if the recovery-mode PR is merged, $REVERT, then rerun"; return 1; }
  bash "$MIG_DIR/restore-target-db.sh" --assert-only || return 1
  state=$("${POD[@]}" sh -c "$PROBE") || { echo "could not read the target-before.dump state from the runner"; return 1; }
  echo "target-before.dump: $state"
  case "$state" in
    fresh) ;;
    missing|stale) retake "$state" || return 1 ;;
    *) echo "unexpected target-before.dump state: $state"; return 1 ;;
  esac
  "${POD[@]}" sh -c 'cd /work && sha256sum target-before.dump' >> "$EVIDENCE_DIR/dumps.sha256" || return 1
  tail -1 "$EVIDENCE_DIR/dumps.sha256"
  bash "$MIG_DIR/restore-target-db.sh" /work/target-before.dump --dry-run | tail -1
}

task_queue() {
  local q="select count(*) from f"
  if no_legacy_db task-queue; then [ "$APPLY" = 0 ]; return; fi
  printf '  + %s\n' "$(shq "${POD[@]}" psql -h "$STALWART_LEGACY_PG_HOST" -U "$STALWART_LEGACY_PG_USER" -d "$STALWART_LEGACY_PG_DB" -Atc "$q")"
  [ "$APPLY" = 1 ] || { echo "  wait up to 600s for 0 pending index tasks (table f), else stop"; return 0; }
  until_ok 600 tasks_empty || { echo "legacy task queue f still holds $VALUE task(s) after 600s; migrate_v0_16 drops them"; return 1; }
  echo "legacy task queue f is empty"
}
tasks_empty() { VALUE=$("${POD[@]}" psql -h "$STALWART_LEGACY_PG_HOST" -U "$STALWART_LEGACY_PG_USER" -d "$STALWART_LEGACY_PG_DB" -Atc "select count(*) from f") && [ "$VALUE" = 0 ]; }

account_ids() {
  local out
  printf '  + . %s; python3 %s > %s/account-ids.txt (PASS required on the account ids and group ids lines)\n' "$MIG_DIR/verify/$TARGET_ENV.env" "$MIG_DIR/verify/02_directory.py" "$S"
  [ "$APPLY" = 1 ] || return 0
  bash -c 'set -a; . "$1"; set +a; cd "$(dirname "$2")" && python3 "$2"' _ "$MIG_DIR/verify/$TARGET_ENV.env" "$MIG_DIR/verify/02_directory.py" > "$S/account-ids.txt" 2>&1 || true
  out=$(grep -E '^  (PASS|FAIL)  (account|group) ids:' "$S/account-ids.txt" | sed -E 's/, missing .*//')
  printf '%s\n' "$out"
  VALUE=$(grep -c '^  FAIL' <<<"$out" || true)
  [ "$(grep -c '^  PASS' <<<"$out" || true)" = 2 ] || { echo "account and group ids differ from legacy or were not checked; see $S/account-ids.txt"; return 1; }
}

blob_recheck() {
  local out rc=0 d
  printf '  + after the W3 blob-check: the delta copy and check of blob-delta.sh again, so blobs written before the legacy dump are in the target\n'
  if [ "$APPLY" = 1 ]; then
    until_ok 3600 blob_ok || { echo "no blob-check differences=0 row for the current cut after 3600s; stay in recovery mode"; return 1; }
  fi
  bash "$MIG_DIR/blob-delta.sh" copy "$S/rclone-recopy-$TS" "--$(mode)" || { echo "delta rclone copy failed; log $S/rclone-recopy-$TS.log"; return 1; }
  out=$(bash "$MIG_DIR/blob-delta.sh" check "$EVIDENCE_DIR/blobs-recheck-$TS" "--$(mode)") || rc=$?
  printf '%s\n' "$out"
  [ "$APPLY" = 1 ] || return 0
  if [ "$rc" != 0 ]; then
    d=$(sed -n 's/.* differences=\([0-9]*\) .*/\1/p' <<<"$out")
    if [ -n "$d" ] && [ "$d" != 0 ]; then VALUE=$d; else VALUE=errors-exit-$rc; fi
    echo "rclone check after the dump failed (exit $rc); see $EVIDENCE_DIR/blobs-recheck-$TS-*.txt"
    return 1
  fi
  VALUE=0
  echo "blobs match after the legacy dump"
}

queue_drain() {
  local body
  if ! legacy_tunnel; then [ "$APPLY" = 0 ] || return 1; NEEDS=$((NEEDS + 1)); return 0; fi
  body=$(curl -sk -m 15 -K <(curl_user "$OLD_STALWART_USER" "$OLD_STALWART_PASSWORD") "$OLD_STALWART_URL/api/queue/messages?limit=1") || { echo "legacy queue API did not answer"; return 1; }
  VALUE=$(jq -r '.data.total // empty' <<<"$body" 2>/dev/null || true)
  echo "legacy queue total=${VALUE:-unreadable}"
  [ -n "$VALUE" ] || return 1
  [ "$APPLY" = 0 ] || [ "$VALUE" = 0 ] || { echo "queue not drained: wait or flush, record it, then rerun"; return 1; }
}

skips() { grep -E "$PRINCIPAL_WARN" "$1" | grep -vcE "$TENANT_SKIP" || true; }

dump() {
  local n
  [ "$APPLY" = 0 ] || legacy_tunnel || return 1
  printf '  + bash %s %s (per-type principal totals: server vs dump)\n' "$MIG_DIR/dump-check.sh" "$S/principals.json"
  run_to "$S/dump.log" "${DUMP_CMD[@]}" || { echo "dump failed; see $S/dump.log"; return 1; }
  if [ "$APPLY" = 0 ]; then
    [ ! -s "$S/dump.log" ] || echo "existing $S/dump.log: $(skips "$S/dump.log") principal type(s) not fetched or skipped besides the community-edition tenant skip"
    return 0
  fi
  for n in settings.json principals.json; do [ -s "$S/$n" ] || { echo "dump wrote no $n"; return 1; }; done
  n=$(skips "$S/dump.log")
  [ "$n" = 0 ] || { echo "dump.log reports $n principal(s) not fetched or skipped besides the community-edition tenant skip; see $S/dump.log"; return 1; }
  bash "$MIG_DIR/dump-check.sh" "$S/principals.json" || { echo "dump-check.sh: the dump does not match the server's principal totals"; return 1; }
}

first_error() { sed -n 's/^error: //p' "$1" | head -1 | sed -E "s/'[^']*'|\"[^\"]*\"/'<value>'/g; s/((User|Group|MailingList|Account) [^ ]+ )\([^)]*\)/\1(<name>)/g; s/[^[:space:]]+@[^[:space:]]+/<address>/g"; }

duplicates() {
  local rc=0
  run_to "$S/duplicates.txt" "$PY" "$MIG_DIR/dup_check.py" --principals "$S/principals.json" --settings "$S/settings.json" || rc=$?
  [ "$APPLY" = 1 ] || return 0
  grep -E $'^(class|total)\t' "$S/duplicates.txt" | tr '\t' ' ' | sed 's/^/  /' || true
  VALUE=$(sed -n $'s/^total\t//p' "$S/duplicates.txt")
  case "$rc" in
    0) ;;
    3) echo "$VALUE duplicate address(es) by class above; each address and its claimants is in $S/duplicates.txt. convert would stop on the first: resolve them on legacy, then rerun from the dump"; return 1 ;;
    *) echo "dup_check.py exit $rc: $(first_error "$S/duplicates.txt"); full text in $S/duplicates.txt"; return 1 ;;
  esac
}

convert() {
  local rc=0
  run_err "$S/convert.stderr" "$PY" "$MIG_DIR/migrate_v016.py" convert --settings "$S/settings.json" --principals "$S/principals.json" --config "$S/config.json" --output "$S/export.json" --unmigrated-output "$S/unmigrated.txt" || rc=$?
  [ "$APPLY" = 1 ] || return 0
  [ "$rc" = 0 ] || { echo "convert exit $rc: $(first_error "$S/convert.stderr"); full text in $S/convert.stderr"; return 1; }
  [ -s "$S/export.json" ] || { echo "convert wrote no export.json"; return 1; }
  VALUE=$(grep -c . "$S/export.json")
  grep -E "$DROP_RE" "$S/convert.stderr" > "$S/convert-dropped.txt" || true
  if [ -s "$DROP_BASELINE" ]; then
    DROPPED=$(grep -vxFf "$DROP_BASELINE" "$S/convert-dropped.txt" | grep -c . || true)
    echo "dropped or renamed accounts: $(grep -c . "$S/convert-dropped.txt" || true), not in the T10 baseline: $DROPPED"
  else
    DROPPED=$(grep -c . "$S/convert-dropped.txt" || true)
  fi
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
  if no_legacy_db db-dump-legacy; then [ "$APPLY" = 0 ]; return; fi
  run "${POD[@]}" pg_dump -Fc --no-owner -h "$STALWART_LEGACY_PG_HOST" -U "$STALWART_LEGACY_PG_USER" -d "$STALWART_LEGACY_PG_DB" -f /work/legacy.dump || return 1
  if [ "$APPLY" = 0 ]; then
    printf '  + %s\n' "$(shq "${POD[@]}" stat -c %s /work/legacy.dump)"
    show_to "$EVIDENCE_DIR/dumps.sha256" "${POD[@]}" sh -c 'cd /work && sha256sum legacy.dump'
    return 0
  fi
  VALUE=$("${POD[@]}" stat -c %s /work/legacy.dump) || return 1
  "${POD[@]}" sh -c 'cd /work && sha256sum legacy.dump' >> "$EVIDENCE_DIR/dumps.sha256" || return 1
  tail -1 "$EVIDENCE_DIR/dumps.sha256"
  [ "$VALUE" -gt 0 ]
}

restore() {
  local out rc n script
  script="pg_restore -j4 --no-owner --no-privileges -h \"\$1\" -U \"\$2\" -d \"\$3\" /work/legacy.dump 2>/work/restore.err; rc=\$?; echo \"pg_restore_exit=\$rc other_errors=\$(grep -v 'schema \"public\" already exists' /work/restore.err | grep -c .)\""
  if [ "$APPLY" = 0 ]; then
    printf '  + bash %s --assert-only, and deploy/stalwart replicas = 0\n' "$MIG_DIR/restore-target-db.sh"
  else
    { bash "$MIG_DIR/restore-target-db.sh" --assert-only && [ "$(replicas)" = 0 ]; } || { echo "assertions failed right before the DROP"; return 1; }
    mark drop-schema host "$STALWART_TARGET_PG_HOST"
  fi
  run "${POD[@]}" psql -h "$STALWART_TARGET_PG_HOST" -U "$STALWART_TARGET_PG_USER" -d "$STALWART_TARGET_PG_DB" -v ON_ERROR_STOP=1 -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' || return 1
  out=$(run "${POD[@]}" sh -c "$script" sh "$STALWART_TARGET_PG_HOST" "$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_DB") || return 1
  printf '%s\n' "$out"
  [ "$APPLY" = 1 ] || return 0
  out=$(tail -1 <<<"$out")
  rc=$(sed -n 's/^pg_restore_exit=\([0-9]*\) .*/\1/p' <<<"$out")
  n=$(sed -n 's/.* other_errors=\([0-9]*\)$/\1/p' <<<"$out")
  [ "$rc" = 0 ] || [ "$n" = 0 ]
}

recovery_boot() {
  printf '  + merge the recovery-mode PR (%s) now; wait up to %ss for: %s; then 900s for schema-version.sh = 00000006\n' "$RECOVERY_PR" "$RECOVERY_WAIT" "$RECOVERY_OK"
  if [ "$APPLY" = 0 ]; then
    recovery_state || true
    echo "  now: $STATE"
    return 0
  fi
  until_ok "$RECOVERY_WAIT" recovery_state || { echo "after ${RECOVERY_WAIT}s not one Ready stalwart pod in recovery mode: $STATE; is $RECOVERY_PR merged and synced by Argo CD?"; return 1; }
  echo "$STATE"
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
  [ "$APPLY" = 1 ] || { env_locked run sed -i '/^STALWART_ADMIN_ACCOUNT_ID=/d' "$ENV_FILE"; setenv STALWART_ADMIN_ACCOUNT_ID '<from the session>'; return 0; }
  id=$(curl -s -m 15 -K <(curl_user "$STALWART_USER" "$STALWART_PASSWORD") "$STALWART_URL/jmap/session" | jq -re '.primaryAccounts["urn:stalwart:jmap"]') || { echo "no urn:stalwart:jmap account in the session"; return 1; }
  [ -n "$id" ] && [ "$id" != null ] || return 1
  env_locked run sed -i '/^STALWART_ADMIN_ACCOUNT_ID=/d' "$ENV_FILE" || { echo "could not drop the old STALWART_ADMIN_ACCOUNT_ID from $ENV_FILE"; return 1; }
  setenv STALWART_ADMIN_ACCOUNT_ID "$id" || { echo "could not append STALWART_ADMIN_ACCOUNT_ID to $ENV_FILE"; return 1; }
  host=$(sed -E 's|^[a-z]+://([^@/]*@)?([^:/]+).*|\2|' <<<"$REDIS_URL")
  grep -qF "$TARGET_BLOB_BUCKET" <<<"$(stalwart-cli get BlobStore)" || { echo "BlobStore read back is not $TARGET_BLOB_BUCKET"; return 1; }
  grep -qF "$host" <<<"$(stalwart-cli get InMemoryStore)" || { echo "InMemoryStore read back is not the REDIS_URL host"; return 1; }
  echo "read back: BlobStore=$TARGET_BLOB_BUCKET InMemoryStore host=$host"
}

recheck_ok() { awk -F'\t' '$1 == "db-dump-legacy" {d = NR} $1 == "blob-recheck" && $4 == "differences" {v = $5; b = NR} END {exit !(d > 0 && b > d && v == "0")}' "$TIMING_TSV"; }

recovery_hold() {
  if [ "$APPLY" = 0 ]; then
    if blob_ok; then echo "blob-check differences=0 for the current cut is in $TIMING_TSV"; else echo "no blob-check differences=0 row after the latest cut-ecs and snap-blobs rows in $TIMING_TSV yet (W2 and W3 write them)"; fi
    printf '  + check again: %s\n' "$RECOVERY_OK"
    return 0
  fi
  recheck_ok || { echo "no blob-recheck differences=0 row after the latest db-dump-legacy row; stay in recovery mode"; return 1; }
  recovery_state || { echo "the recovery-mode state changed before the end of W4: $STATE"; return 1; }
  echo "$STATE"
  echo "W4 ends in recovery mode; leaving it is a PR (W 5.1), merged only after the blob-recheck differences=0 row above"
}

RULE="length(k)=44 and length(v)=1 and (case when length(v)=1 then get_byte(v,0) end) in (0,2) and (case when length(k)=44 then ('x'||encode(substring(k from 37 for 8),'hex'))::bit(64)::bigint end) > extract(epoch from now())::bigint"
TPSQL=(psql -h "${STALWART_TARGET_PG_HOST:-}" -U "${STALWART_TARGET_PG_USER:-}" -d "${STALWART_TARGET_PG_DB:-}" -v ON_ERROR_STOP=1 -At)
NB=(python3 "$MIG_DIR/neon_branch.py" --project "${NEON_PROJECT_STALWART_LEGACY:-}" --parent "${NEON_LEGACY_STALWART_BRANCH:-}")
WANT_SCHEMA=${STALWART_SCHEMA_VERSION:-00000006}
cut_row() { awk -F'\t' -v s="$1" '$1 == "cut-ecs" {l = ""; n = 0} $1 == s {l = $0; n = NR} END {if (l != "") print n "\t" l}' "$TIMING_TSV" 2>/dev/null; }
cut_time() { cut_row "$1" | cut -f4; }
cut_line() { cut_row "$1" | cut -f1; }
apply_flag() { [ "$APPLY" = 1 ] && echo --apply || echo --dry-run; }
APPLY_ARG=()
neon_need() { [ "$1" = 0 ] || [ "$APPLY" = 1 ] || { printf 'NEED  %s: the Neon read failed (dry run continues)\n' "$2"; NEEDS=$((NEEDS + 1)); }; }

domain_fix_step() {
  local rc=0
  run_to "$S/domain-fix.txt" "$PY" "$MIG_DIR/domain_fix.py" --export "$S/export.json" --principals "$S/principals.json" --dropped-list "$S/domains-dropped.json" || rc=$?
  [ "$APPLY" = 1 ] || return 0
  [ "$rc" = 0 ] || { echo "domain_fix.py exit $rc: $(first_error "$S/domain-fix.txt"); full text in $S/domain-fix.txt"; return 1; }
  grep '^domains checked=' "$S/domain-fix.txt" || true
  grep '^warning: ' "$S/domain-fix.txt" > "$S/domain-fixed.txt" || true
  VALUE=$(grep -c . "$S/domain-fixed.txt" || true)
  echo "domains: $VALUE warning line(s), one per dropped Domain and DkimSignature ($S/domain-fixed.txt)"
}

read_done() {
  [ -s "$S/export.json" ] || { echo "no $S/export.json"; return 1; }
  mark w4-read-done export "$S/export.json"
  echo "legacy-read phase done: $S/export.json; legacy Stalwart may be stopped now (W 4.3)"
}

export_ok() {
  local e s
  e=$(cut_line w4-export) s=$(cut_line w4-export-start)
  [ -n "$e" ] && { [ -z "$s" ] || [ "$e" -gt "$s" ]; } && [ -s "$S/export.json" ]
}

read_phase() {
  if [ "$APPLY" = 1 ] && [ -z "$GO" ] && export_ok; then
    echo "reusing the export of this cut (w4-export row, $S/export.json): only queue-drain runs; --go '<words>' dumps again"
  else
    [ "$APPLY" = 0 ] || mark w4-export-start file "$S/export.json"
    step dump "" dump
    step duplicates duplicates duplicates
    step convert export_lines convert
    if [ "$APPLY" = 1 ]; then
      again convert dropped_accounts "$DROPPED" || stop "could not write the convert dropped_accounts row"
      [ "$DROPPED" = 0 ] || stop "convert.stderr reports $DROPPED dropped or renamed User/Group line(s) outside the T10 baseline ($S/convert-dropped.txt)"
    fi
    step domains changes domain_fix_step
    step retarget "" retarget
    [ "$APPLY" = 0 ] || mark w4-export file "$S/export.json"
  fi
  step queue-drain queued queue_drain
  [ "$APPLY" = 0 ] || step read-done "" read_done
}

no_stalwart() {
  local r n
  r=$(replicas) || { echo "deploy/stalwart not readable"; return 1; }
  n=$("${KS[@]}" "${NS[@]}" get pods -l app=stalwart -o json | jq '.items | length') || { echo "stalwart pods not readable"; return 1; }
  echo "deploy/stalwart replicas=$r, app=stalwart pods=$n"
  [ "$r" = 0 ] && [ "$n" = 0 ]
}

reset_preflight() {
  local read stop bad=0
  read=$(cut_line w4-read-done) stop=$(cut_line stop-legacy-stalwart)
  STOP_AT=$(cut_time stop-legacy-stalwart)
  if [ -n "$read" ] && ! export_ok; then echo "the export of the last read run did not pass its checks (no w4-export row after the last w4-export-start row)"; read=""; fi
  if ! awk -F'\t' '$1 == "cut-ecs" {f = 1} END {exit !f}' "$TIMING_TSV" 2>/dev/null; then echo "no cut-ecs row in $TIMING_TSV: W2 has not cut legacy in this attempt"; bad=1; fi
  if [ -z "$read" ] || [ ! -s "$S/export.json" ]; then echo "no w4-read-done row after the last cut-ecs row (or no $S/export.json): run --phase read first"; bad=1; fi
  if [ -z "$stop" ]; then echo "no stop-legacy-stalwart row after the last cut-ecs row: legacy Stalwart is not recorded stopped (W 4.3)"; bad=1
  elif [ -n "$read" ] && [ "$stop" -lt "$read" ]; then echo "the stop-legacy-stalwart row comes before the w4-read-done row: stop legacy Stalwart after the read phase"; bad=1
  else echo "legacy Stalwart stopped at $STOP_AT (stop-legacy-stalwart row)"; fi
  if runner_up; then echo "runner Running"; else echo "runner pod is not Running"; bad=1; fi
  no_stalwart || { echo "the reset needs deploy/stalwart at 0 replicas and no app=stalwart pod"; bad=1; }
  [ "$bad" = 0 ] && return 0
  [ "$APPLY" = 1 ] && return 1
  printf 'NEED  the reset phase runs after the read phase, the legacy stop row and with no Stalwart pod (dry run continues)\n'
  NEEDS=$((NEEDS + 1))
  STOP_AT=${STOP_AT:-1970-01-01T00:00:00Z}
}

neon_reset() {
  local out rc=0
  printf '  + %s reset --branch %s --after %s %s\n' "$(shq "${NB[@]}")" "$CUTOVER_BRANCH" "$STOP_AT" "$(apply_flag)"
  out=$("${NB[@]}" reset --branch "$CUTOVER_BRANCH" --after "$STOP_AT" "${APPLY_ARG[@]+"${APPLY_ARG[@]}"}") || rc=$?
  printf '%s\n' "$out"
  neon_need "$rc" "neon_branch.py reset"
  [ "$APPLY" = 1 ] || return 0
  [ "$rc" = 0 ] || return 1
  VALUE=$(sed -n 's/^after: .* last_reset_at=\([^ ]*\) .*/\1/p' <<<"$out")
  mark reset last_reset_at "$VALUE"
}

branch_check() {
  local out rc=0
  printf '  + %s check --cutover %s --source %s --after %s\n' "$(shq "${NB[@]}")" "$CUTOVER_BRANCH" "$SOURCE_BRANCH" "$STOP_AT"
  out=$("${NB[@]}" check --cutover "$CUTOVER_BRANCH" --source "$SOURCE_BRANCH" --after "$STOP_AT") || rc=$?
  printf '%s\n' "$out"
  neon_need "$rc" "neon_branch.py check"
  [ "$APPLY" = 1 ] || return 0
  VALUE=$(sed -n 's/^branch check: .* parent_lsn \(.*\)$/\1/p' <<<"$out")
  [ "$rc" = 0 ]
}

source_branch() {
  local out rc=0 f=$S/source-branch.env n v
  printf '  + %s source --name %s --role %s --db %s --out %s --after %s %s\n' "$(shq "${NB[@]}")" "$SOURCE_BRANCH" "$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_DB" "$f" "$STOP_AT" "$(apply_flag)"
  out=$("${NB[@]}" source --name "$SOURCE_BRANCH" --role "$STALWART_TARGET_PG_USER" --db "$STALWART_TARGET_PG_DB" --out "$f" --after "$STOP_AT" "${APPLY_ARG[@]+"${APPLY_ARG[@]}"}") || rc=$?
  printf '%s\n' "$out"
  neon_need "$rc" "neon_branch.py source"
  if [ "$APPLY" = 0 ]; then
    for n in STALWART_SOURCE_PG_HOST STALWART_SOURCE_PG_DB STALWART_SOURCE_PG_USER STALWART_SOURCE_PG_PASSWORD; do printf '  + replace %s in %s from %s\n' "$n" "$ENV_FILE" "$f"; done
    return 0
  fi
  [ "$rc" = 0 ] && [ -s "$f" ] || return 1
  for n in STALWART_SOURCE_PG_HOST STALWART_SOURCE_PG_DB STALWART_SOURCE_PG_USER STALWART_SOURCE_PG_PASSWORD; do
    v=$(sed -n "s/^$n=//p" "$f" | tail -1)
    [ -n "$v" ] || { echo "$f has no $n"; return 1; }
    printf -v "$n" '%s' "$v"
  done
  env_locked source_env_write || { echo "could not replace STALWART_SOURCE_PG_* in $ENV_FILE"; return 1; }
  for n in STALWART_SOURCE_PG_HOST STALWART_SOURCE_PG_DB STALWART_SOURCE_PG_USER; do window_value "$n" "${!n}" || true; done
  printf '  + replaced STALWART_SOURCE_PG_HOST=%s STALWART_SOURCE_PG_DB=%s STALWART_SOURCE_PG_USER=%s STALWART_SOURCE_PG_PASSWORD (len=%s) in %s\n' "$STALWART_SOURCE_PG_HOST" "$STALWART_SOURCE_PG_DB" "$STALWART_SOURCE_PG_USER" "${#STALWART_SOURCE_PG_PASSWORD}" "$ENV_FILE"
  export STALWART_SOURCE_PG_HOST STALWART_SOURCE_PG_DB STALWART_SOURCE_PG_USER STALWART_SOURCE_PG_PASSWORD
  VALUE=$(sed -n 's/^created: .* created_at=\([^ ]*\) .*/\1/p' <<<"$out")
  mark source-branch created_at "$VALUE"
}

source_env_write() {
  local n q
  sed -i '/^STALWART_SOURCE_PG_\(HOST\|DB\|USER\|PASSWORD\)=/d' "$ENV_FILE" || return 1
  for n in STALWART_SOURCE_PG_HOST STALWART_SOURCE_PG_DB STALWART_SOURCE_PG_USER STALWART_SOURCE_PG_PASSWORD; do
    q=$(printf '%s' "${!n}" | sed "s/'/'\\\\''/g")
    env_append "$n" "$q" || return 1
  done
}

schema_before() {
  printf '  + bash %s (want 00000005 on the reset branch)\n' "$MIG_DIR/schema-version.sh"
  [ "$APPLY" = 1 ] || return 0
  VALUE=$(bash "$MIG_DIR/schema-version.sh" 2>/dev/null) || VALUE=unreadable
  echo "schema on $CUTOVER_BRANCH: $VALUE"
  [ "$VALUE" = 00000005 ] || { echo "schema $VALUE after the reset, want 00000005: not a fresh copy of legacy; nothing deleted"; return 1; }
}

auth_failed() { grep -q 'password authentication failed' <<<"$1" && grep -q '^rc=[1-9]' <<<"$1"; }

guard() {
  local hosts parent branch out pw
  printf '  + %s hosts --branch %s\n' "$(shq "${NB[@]}")" "$CUTOVER_BRANCH"
  printf '  + runner: %s -c %s (cutover branch login through .pgpass)\n' "$(shq "${TPSQL[@]}")" "'select 1'"
  printf '  + runner: the same credential against the production endpoint: pass only on password authentication failed\n'
  printf '  + runner: the %s credential: login on its host, then password authentication failed on production\n' "$SOURCE_BRANCH"
  [ "$APPLY" = 1 ] || return 0
  hosts=$("${NB[@]}" hosts --branch "$CUTOVER_BRANCH") || { printf '%s\n' "$hosts"; return 1; }
  parent=$(sed -n 's/^parent_host=//p' <<<"$hosts") branch=$(sed -n 's/^branch_host=//p' <<<"$hosts")
  [ -n "$parent" ] && [ "$parent" != "$STALWART_TARGET_PG_HOST" ] || { echo "STALWART_TARGET_PG_HOST is the production endpoint or the production host is unknown"; return 1; }
  [ "$branch" = "$STALWART_TARGET_PG_HOST" ] || { echo "STALWART_TARGET_PG_HOST is not the endpoint of $CUTOVER_BRANCH"; return 1; }
  out=$("${POD[@]}" "${TPSQL[@]}" -c 'select 1' 2>&1) || true
  [ "$out" = 1 ] || { echo "cutover branch login failed: $(tail -1 <<<"$out")"; return 1; }
  echo "cutover branch login: ok"
  out=$("${POD[@]}" sh -c 'umask 077; awk -F: -v h="$1" -v p="$2" '\''BEGIN {OFS = ":"} $1 == h && $2 == "5432" {$1 = p; print; exit}'\'' /secrets/.pgpass > /secrets/.guard; [ -s /secrets/.guard ] || { echo "no .pgpass line for $1"; echo rc=99; exit 0; }; PGPASSFILE=/secrets/.guard psql -h "$2" -U "$3" -d "$4" -Atc "select 1" 2>&1; echo "rc=$?"; rm -f /secrets/.guard' sh "$STALWART_TARGET_PG_HOST" "$parent" "$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_DB")
  auth_failed "$out" || { echo "GUARD FAILED: production did not reject the cutover credential with password authentication failed: $(grep -v '^rc=' <<<"$out" | tail -1) ($(grep '^rc=' <<<"$out"))"; return 1; }
  echo "production rejects the cutover credential: password authentication failed"
  pw=${STALWART_SOURCE_PG_PASSWORD:-}
  [ -n "$pw" ] && [ -n "${STALWART_SOURCE_PG_HOST:-}" ] || { echo "no source branch credential in the environment"; return 1; }
  for h in "$STALWART_SOURCE_PG_HOST" "$parent"; do
    out=$(printf '%s\n' "$pw" | "${POD[@]}" sh -c 'IFS= read -r PGPASSWORD; export PGPASSWORD; psql -h "$1" -U "$2" -d "$3" -Atc "select 1" 2>&1; echo "rc=$?"' sh "$h" "$STALWART_SOURCE_PG_USER" "$STALWART_SOURCE_PG_DB")
    if [ "$h" = "$parent" ]; then
      auth_failed "$out" || { echo "GUARD FAILED: production did not reject the $SOURCE_BRANCH credential with password authentication failed: $(grep -v '^rc=' <<<"$out" | tail -1) ($(grep '^rc=' <<<"$out"))"; return 1; }
      echo "production rejects the $SOURCE_BRANCH credential: password authentication failed"
    else
      [ "$(head -1 <<<"$out")" = 1 ] && grep -qx 'rc=0' <<<"$out" || { echo "$SOURCE_BRANCH login failed: $(grep -v '^rc=' <<<"$out" | tail -1)"; return 1; }
      echo "$SOURCE_BRANCH login: ok"
    fi
  done
  mark guard result pass
  echo "GUARD PASS"
}

predelete() {
  local before out after n
  printf '  + runner, one transaction: %s -1 -c %s -c %s -c %s\n' "$(shq "${TPSQL[@]}")" "$(shq "select count(*) from k where $RULE")" "$(shq "delete from k where $RULE")" "$(shq "select count(*) from k where $RULE")"
  [ "$APPLY" = 1 ] || return 0
  no_stalwart || { echo "a Stalwart pod exists or deploy/stalwart is not at 0: no pre-delete"; return 1; }
  out=$("${POD[@]}" "${TPSQL[@]}" -1 -c "select count(*) from k where $RULE" -c "delete from k where $RULE" -c "select count(*) from k where $RULE") || return 1
  before=$(sed -n 1p <<<"$out") after=$(sed -n 3p <<<"$out")
  n=$(sed -n '2s/^DELETE \([0-9]*\)$/\1/p' <<<"$out")
  echo "links to delete: $before"
  sed -n 2p <<<"$out"
  echo "$after"
  VALUE=$n
  [ -n "$n" ] && [ "$n" = "$before" ] && [ "$after" = 0 ] || { echo "the pre-delete removed ${n:-no} row(s) of $before, $after left"; return 1; }
  mark pre-delete deleted "$n"
}

pod_after() {
  local t
  t=$("${KS[@]}" "${NS[@]}" get pods -l app=stalwart -o json | jq -r '[.items[].status.startTime // empty] | min // empty')
  [ -n "$t" ] && [[ "$t" > "$PRE_AT" ]] || { STATE="$STATE; pod start ${t:-none} is not later than the pre-delete at $PRE_AT"; return 1; }
}
boot_ready() { recovery_state && pod_after; }
prod_schema_ok() { [ "$(bash "$MIG_DIR/schema-version.sh" 2>/dev/null)" = "$WANT_SCHEMA" ]; }

prod_recovery_boot() {
  printf '  + merge the recovery-mode PR (%s) now; wait up to %ss for: %s, started after the pre-delete; then 900s for schema-version.sh = %s\n' "$RECOVERY_PR" "$RECOVERY_WAIT" "$RECOVERY_OK" "$WANT_SCHEMA"
  if [ "$APPLY" = 0 ]; then
    recovery_state || true
    echo "  now: $STATE"
    return 0
  fi
  PRE_AT=$(cut_time pre-delete)
  mark recovery-wait seconds "$RECOVERY_WAIT"
  until_ok "$RECOVERY_WAIT" boot_ready || { echo "after ${RECOVERY_WAIT}s not one Ready stalwart pod in recovery mode started after the pre-delete: $STATE; is $RECOVERY_PR merged and synced by Argo CD? resume with: bash $0 --phase boot --apply"; return 1; }
  echo "$STATE"
  until_ok 900 prod_schema_ok || { echo "schema version is $(bash "$MIG_DIR/schema-version.sh" 2>/dev/null || echo unreadable), not $WANT_SCHEMA, after 900s; resume with: bash $0 --phase boot --apply"; return 1; }
  VALUE=$WANT_SCHEMA
}

prod_apply() {
  local a p
  a=$(cut_line export-applied) p=$(cut_line pre-delete)
  if [ "$APPLY" = 1 ] && [ -n "$a" ] && [ -n "$p" ] && [ "$a" -gt "$p" ]; then echo "the export is already applied after this pre-delete (export-applied row)"; VALUE=0; return 0; fi
  apply_export || return 1
  [ "$APPLY" = 0 ] || mark export-applied errors 0
}

prod_recovery_hold() {
  printf '  + check again: %s\n' "$RECOVERY_OK"
  [ "$APPLY" = 1 ] || return 0
  recovery_state || { echo "the recovery-mode state changed before the end of W4: $STATE"; return 1; }
  echo "$STATE"
  echo "W4 ends in recovery mode; leaving it is a PR (W 5.1)"
}

boot_resume() {
  local r g p
  r=$(cut_line reset) g=$(cut_line guard) p=$(cut_line pre-delete)
  [ -n "$r" ] && [ -n "$g" ] && [ -n "$p" ] && [ "$g" -gt "$r" ] && [ "$p" -gt "$g" ] || { echo "nothing to resume: want reset, guard and pre-delete rows, in that order, after the last cut-ecs row"; return 1; }
  echo "resuming after the pre-delete of $(cut_time pre-delete); no second reset"
}

need_recovery_pr() {
  [ -z "${TM_RECOVERY_PR:-}" ] || return 0
  printf 'NEED  TM_RECOVERY_PR is empty: export the number of the recovery-mode PR before W4\n'
  [ "$APPLY" = 1 ] && stop "TM_RECOVERY_PR is empty"
  NEEDS=$((NEEDS + 1))
}

reset_phase() {
  STOP_AT=""
  [ "$APPLY" = 0 ] || APPLY_ARG=(--apply)
  step reset-preflight "" reset_preflight
  if [ "$APPLY" = 1 ] && [ -n "$(cut_row reset)" ]; then
    [ -n "$GO" ] || stop "$CUTOVER_BRANCH was already reset in this cut: a second reset needs the operator's confirmation (--go); after an expired pod wait use --phase boot"
    go_gate
  fi
  step neon-reset reset_at neon_reset
  step source-branch-create created_at source_branch
  step branch-check parent_lsn branch_check
  step schema-before schema schema_before
  step guard-check "" guard
  step predelete deleted predelete
}

boot_phase() {
  step recovery-boot schema prod_recovery_boot
  step apply errors prod_apply
  step admin-account-id "" admin_id
  step account-ids fails account_ids
  step recovery-hold "" prod_recovery_hold
}

prod_main() {
  case "$PHASE" in
    read)
      GATE_ID=go-W4-read prereqs
      helpers migrate_v016.py dump_legacy.py dup_check.py domain_fix.py domains.py domain-suffixes.txt dump-check.sh retarget-export.sh "$PY"
      read_phase ;;
    reset|boot)
      GATE_ID=go-W4-reset prereqs
      helpers neon_branch.py schema-version.sh verify/02_directory.py verify/$TARGET_ENV.env stalwart-cli python3 "$PY"
      need_recovery_pr
      if [ "$PHASE" = reset ]; then reset_phase; else [ "$APPLY" = 0 ] || boot_resume || stop "--phase boot has nothing to resume"; fi
      boot_phase ;;
    *)
      [ "$APPLY" = 0 ] || { echo "on tb-prod --apply needs --phase read, --phase reset or --phase boot"; exit 64; }
      GATE_ID=go-W4-read prereqs
      GATE_ID=go-W4-reset prereqs
      helpers migrate_v016.py dump_legacy.py dup_check.py domain_fix.py domains.py domain-suffixes.txt dump-check.sh retarget-export.sh neon_branch.py schema-version.sh verify/02_directory.py verify/$TARGET_ENV.env stalwart-cli python3 "$PY"
      need_recovery_pr
      read_phase
      reset_phase
      boot_phase ;;
  esac
}

if [ "$TARGET_ENV" = tb-prod ]; then
  prod_main
  finish
  exit 0
fi

prereqs
helpers migrate_v016.py dup_check.py blob-delta.sh dump-check.sh retarget-export.sh schema-version.sh restore-target-db.sh verify/02_directory.py verify/$TARGET_ENV.env stalwart-cli rclone "$PY"
if [ -z "${TM_RECOVERY_PR:-}" ]; then
  printf 'NEED  TM_RECOVERY_PR is empty: export the number of the recovery-mode PR before W4\n'
  [ "$APPLY" = 1 ] && stop "TM_RECOVERY_PR is empty"
  NEEDS=$((NEEDS + 1))
fi
if [ "$APPLY" = 1 ] && [ -n "$(after_cut drop-schema)" ]; then
  [ -n "$GO" ] || stop "DROP SCHEMA already started for this cut: a second DROP needs the operator's confirmation (--go)"
  go_gate
fi
step w4-preflight "" preflight
step queue-drain queued queue_drain
step task-queue tasks task_queue
step dump "" dump
step duplicates duplicates duplicates
step convert export_lines convert
if [ "$APPLY" = 1 ]; then
  again convert dropped_accounts "$DROPPED" || stop "could not write the convert dropped_accounts row"
  [ "$DROPPED" = 0 ] || stop "convert.stderr reports $DROPPED dropped or renamed User/Group line(s) outside the T10 baseline ($S/convert-dropped.txt)"
fi
step retarget "" retarget
step db-dump-legacy bytes dump_legacy
step db-restore "" restore
step recovery-boot schema recovery_boot
step apply errors apply_export
step admin-account-id "" admin_id
step account-ids fails account_ids
step blob-recheck differences blob_recheck
step recovery-hold "" recovery_hold
finish
