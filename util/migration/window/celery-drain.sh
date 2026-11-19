#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ "$TARGET_ENV" = tb-prod ] || { echo "celery-drain.sh reads the legacy Accounts Celery of the tb-prod cutover; TARGET_ENV=$TARGET_ENV has none" >&2; exit 64; }
CMD=${POS[0]:-}
case "$CMD:${#POS[@]}" in
  read:1|wait:1) ;;
  *) echo "usage: celery-drain.sh read|wait [--dry-run | --apply]" >&2; exit 64 ;;
esac
need LEGACY_ECS_TARGETS LEGACY_ECS_CLUSTER_PREFIX
RO=(aws --profile mzla-legacy-ro --region "$AWS_REGION")
TIMEOUT=${CELERY_DRAIN_TIMEOUT:-900}
INTERVAL=${CELERY_DRAIN_INTERVAL:-30}
LOOKBACK=${CELERY_DRAIN_LOOKBACK:-7200}
FRESH=${CELERY_DRAIN_FRESH:-180}
W2_RESTORE=$EVIDENCE_DIR/cut-legacy-restore.txt
read -ra CELERY <<<"$(tr ' ' '\n' <<<"$LEGACY_ECS_TARGETS" | grep -- '-celery-' | tr '\n' ' ' || :)"
[ "${#CELERY[@]}" = 1 ] || stop "LEGACY_ECS_TARGETS names ${#CELERY[@]} Celery services, not 1: $LEGACY_ECS_TARGETS"
CL=${CELERY[0]%%/*}
SV=${CELERY[0]##*/}

log_group() {
  local g
  g=$("${RO[@]}" logs describe-log-groups --log-group-name-prefix "$LEGACY_ECS_CLUSTER_PREFIX" --query 'logGroups[].logGroupName' --output text) || { echo "logs describe-log-groups failed"; return 1; }
  read -ra g <<<"$(tr '\t' '\n' <<<"$g" | grep -- '-celery-' | tr '\n' ' ' || :)"
  [ "${#g[@]}" = 1 ] || { echo "log groups $LEGACY_ECS_CLUSTER_PREFIX* with -celery-: ${#g[@]}, not 1"; return 1; }
  LOG_GROUP=${g[0]}
}

events() {
  local s=$1 since=$2 tok="" out next n
  while :; do
    out=$("${RO[@]}" logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "$s" --start-time "$since" --start-from-head ${tok:+--next-token "$tok"} --output json) || return 1
    jq -r '.events[] | "\(.timestamp) \(.message | gsub("[\r\n]"; " "))"' <<<"$out"
    n=$(jq '.events | length' <<<"$out")
    next=$(jq -r '.nextForwardToken // ""' <<<"$out")
    [ "$n" -gt 0 ] && [ -n "$next" ] && [ "$next" != "$tok" ] || return 0
    tok=$next
  done
}

held_count() {
  sort -s -n -k1,1 | awk -v now="$1" '
    match($0, /Task [A-Za-z0-9_.:]+\[[0-9a-f-]+\] [a-z]+/) {
      m = substr($0, RSTART + 5, RLENGTH - 5); i = index(m, "["); j = index(m, "]")
      id = substr(m, i + 1, j - i - 1); last[id] = substr(m, j + 2); name[id] = substr(m, 1, i - 1); at[id] = $1
      if (last[id] == "received") r++; else if (last[id] != "retry") f++
    }
    match($0, /Discarding revoked task: [A-Za-z0-9_.:]+\[[0-9a-f-]+\]/) {
      m = substr($0, RSTART, RLENGTH); i = index(m, "["); last[substr(m, i + 1, length(m) - i - 1)] = "revoked"; f++
    }
    $1 + 0 > newest { newest = $1 + 0 }
    END {
      for (id in last) if (last[id] == "received" || last[id] == "retry") { h++; printf "  held %s %s %ds\n", name[id], last[id], (now - at[id]) / 1000 }
      printf "%d %d %d %d\n", h, r, f, newest ? (now - newest) / 1000 : -1
    }'
}

celery_read() {
  local svc desired running tasks t s streams now since out sum held rec fin age
  svc=$("${RO[@]}" ecs describe-services --cluster "$CL" --services "$SV" --query 'services[0].[desiredCount,runningCount]' --output text) || { echo "ecs describe-services $CL/$SV failed"; return 1; }
  read -r desired running <<<"$svc"
  tasks=$("${RO[@]}" ecs list-tasks --cluster "$CL" --service-name "$SV" --desired-status RUNNING --query 'taskArns[]' --output text) || { echo "ecs list-tasks $CL/$SV failed"; return 1; }
  read -ra tasks <<<"$(tr '\t' ' ' <<<"$tasks")"
  streams=$("${RO[@]}" logs describe-log-streams --log-group-name "$LOG_GROUP" --order-by LastEventTime --descending --max-items 50 --query 'logStreams[].logStreamName' --output text) || { echo "logs describe-log-streams failed"; return 1; }
  now=$(( $(date +%s) * 1000 ))
  since=$(( now - LOOKBACK * 1000 ))
  out=$(for t in "${tasks[@]}"; do
    s=$(tr '\t' '\n' <<<"$streams" | grep -m1 -- "/${t##*/}\$" || :)
    [ -n "$s" ] || { echo "MISSING ${t##*/}"; continue; }
    events "$s" "$since" || echo "FAILED $s"
  done) || return 1
  if grep -q '^MISSING ' <<<"$out"; then echo "no log stream in $LOG_GROUP for running Celery task(s): $(sed -n 's/^MISSING //p' <<<"$out" | tr '\n' ' ')"; return 1; fi
  if grep -q '^FAILED ' <<<"$out"; then echo "logs get-log-events failed for $(sed -n 's/^FAILED //p' <<<"$out" | tr '\n' ' ')"; return 1; fi
  sum=$(held_count "$now" <<<"$out")
  sed '$d' <<<"$sum"
  read -r held rec fin age <<<"$(tail -1 <<<"$sum")"
  printf 'celery held=%s received=%s finished=%s newest_log_age=%ss desired=%s running=%s tasks=%s lookback=%ss\n' "$held" "$rec" "$fin" "$age" "$desired" "$running" "${#tasks[@]}" "$LOOKBACK"
  HELD=$held
  READ_OK=0
  [ "$desired" -gt 0 ] && [ "$running" = "$desired" ] && [ "${#tasks[@]}" = "$running" ] && [ "$age" -ge 0 ] && [ "$age" -le "$FRESH" ] && READ_OK=1
  [ "$READ_OK" = 1 ] || echo "  not a drained read: needs desired=running=tasks above 0 and a log line in the last ${FRESH}s"
}

ecs_snapshot() {
  local f
  [ -s "$W2_RESTORE" ] || { echo "no $W2_RESTORE: run 1.5 (w2-cut-legacy.sh --apply) first"; return 1; }
  [ -n "$(after_cut restore-W2)" ] || { echo "no restore-W2 row after the last cut-ecs row in $TIMING_TSV: run 1.5 (w2-cut-legacy.sh --apply) first"; return 1; }
  f=$(awk '$2 ~ /restore-ecs\.sh$/ {print $3; exit}' "$W2_RESTORE")
  [ -n "$f" ] && [ -s "$f" ] || { echo "$W2_RESTORE names no ECS snapshot that exists"; return 1; }
  ECS_BEFORE=$f
  echo "ECS snapshot of 1.5: $ECS_BEFORE"
}

same_desired() {
  local t want have bad=0
  for t in $LEGACY_ECS_TARGETS; do
    want=$(jq -r --arg c "${t%%/*}" --arg s "${t##*/}" '[.services[] | select(.cluster == $c and .serviceName == $s) | .desiredCount][0] // "none"' "$ECS_BEFORE")
    have=$("${RO[@]}" ecs describe-services --cluster "${t%%/*}" --services "${t##*/}" --query 'services[0].desiredCount' --output text) || { echo "ecs describe-services $t failed"; return 1; }
    printf '  %s desired=%s saved=%s\n' "$t" "$have" "$want"
    [ "$have" = "$want" ] || bad=1
  done
  [ "$bad" = 0 ] || { echo "a desired count differs from the 1.5 snapshot $ECS_BEFORE"; return 1; }
  echo "desired counts equal the 1.5 snapshot"
}

plan() {
  printf '== %s %s: read-only, profile mzla-legacy-ro; no desired count, task or broker is changed\n' "$W" "$CMD"
  [ "$CMD" = read ] || printf '  every %ss until held=0, at most %ss:\n' "$INTERVAL" "$TIMEOUT"
  printf '  + %s\n' "$(shq "${RO[@]}" logs describe-log-groups --log-group-name-prefix "$LEGACY_ECS_CLUSTER_PREFIX" --query 'logGroups[].logGroupName' --output text)"
  printf '  + %s\n' "$(shq "${RO[@]}" ecs describe-services --cluster "$CL" --services "$SV" --query 'services[0].[desiredCount,runningCount]' --output text)"
  printf '  + %s\n' "$(shq "${RO[@]}" ecs list-tasks --cluster "$CL" --service-name "$SV" --desired-status RUNNING --query 'taskArns[]' --output text)"
  printf '  + %s\n' "$(shq "${RO[@]}" logs describe-log-streams --log-group-name '<celery log group>' --order-by LastEventTime --descending --max-items 50 --query 'logStreams[].logStreamName' --output text)"
  printf '  + %s   (one per running task, the last %ss, paged)\n' "$(shq "${RO[@]}" logs get-log-events --log-group-name '<celery log group>' --log-stream-name '<stream of the task>' --start-time '<now-lookback>' --start-from-head --output json)" "$LOOKBACK"
  [ "$CMD" = read ] || printf '  + %s   (for each of LEGACY_ECS_TARGETS, against the 1.5 snapshot, before and after)\n' "$(shq "${RO[@]}" ecs describe-services --cluster '<cluster>' --services '<service>' --query 'services[0].desiredCount' --output text)"
}

plan
if [ "$CMD" = wait ]; then
  if out=$(ecs_snapshot); then printf '%s\n' "$out"
  elif [ "$APPLY" = 1 ]; then stop "$out"
  else printf 'NEED  %s\n' "$out"; NEEDS=$((NEEDS + 1)); fi
fi
[ "$APPLY" = 1 ] || { finish; exit 0; }

log_group || stop "no single Celery log group"
echo "log group: $LOG_GROUP"
if [ "$CMD" = read ]; then
  celery_read || stop "celery read failed"
  finish
  exit 0
fi

ECS_BEFORE=$(sed -n 's/^ECS snapshot of 1.5: //p' <<<"$out")
same_desired || stop "desired counts differ from the 1.5 snapshot before the wait"
end=$(( $(date +%s) + TIMEOUT ))
while :; do
  HELD="" READ_OK=0
  celery_read || stop "celery read failed"
  [ "$READ_OK" = 1 ] && [ "$HELD" = 0 ] && break
  [ "$(date +%s)" -lt "$end" ] || stop "held=${HELD:-?} after ${TIMEOUT}s: the legacy Celery workers still hold tasks or their logs are stale"
  sleep "$INTERVAL"
done
same_desired || stop "desired counts differ from the 1.5 snapshot after the wait"
mark celery-drain held 0
echo "celery drained: held=0, desired counts unchanged"
finish
