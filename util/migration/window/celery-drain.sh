#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
case ${TARGET_ENV:-tb-prod} in tb-prod) ;; *) { echo "celery-drain.sh reads the legacy Accounts Celery of the tb-prod cutover; TARGET_ENV=${TARGET_ENV:-} has none" >&2; exit 64; } ;; esac
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
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
for v in "$TIMEOUT" "$INTERVAL" "$LOOKBACK" "$FRESH"; do [[ $v =~ ^[0-9]+$ ]] || { echo "CELERY_DRAIN_TIMEOUT, _INTERVAL, _LOOKBACK and _FRESH take whole seconds (got '$v')" >&2; exit 64; }; done
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
    printf '%s' "$out" | jq -r '.events[] | [(.timestamp | tostring)] + (.message | first(capture("Task (?<n>[A-Za-z0-9_.:]+)\\[(?<i>[0-9a-f-]+)\\] (?<w>[a-z]+)"), (capture("Discarding revoked task: (?<n>[A-Za-z0-9_.:]+)\\[(?<i>[0-9a-f-]+)\\]") | .w = "revoked"), {n: "-", i: "-", w: "-"}) | [.n, .i, .w]) | join(" ")' || return 1
    n=$(printf '%s' "$out" | jq '.events | length') || return 1
    next=$(printf '%s' "$out" | jq -r '.nextForwardToken // ""') || return 1
    [ "$n" -gt 0 ] && [ -n "$next" ] && [ "$next" != "$tok" ] || return 0
    tok=$next
  done
}

held_count() {
  sort -s -n -k1,1 | awk -v now="$1" '
    $1 + 0 > newest { newest = $1 + 0 }
    $4 == "-" || NF != 4 { next }
    { last[$3] = $4; name[$3] = $2; at[$3] = $1; if ($4 == "received") r++; else if ($4 != "retry") f++ }
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
  printf '== %s %s: read-only, profile mzla-legacy-ro; changes no desired count, task or broker\n' "$W" "$CMD"
  printf '  + %s\n' "$(shq "${RO[@]}" logs describe-log-groups --log-group-name-prefix "$LEGACY_ECS_CLUSTER_PREFIX" --query 'logGroups[].logGroupName' --output text)"
  if [ "$CMD" = wait ]; then
    printf '  + %s   (for each of LEGACY_ECS_TARGETS, against the 1.5 snapshot)\n' "$(shq "${RO[@]}" ecs describe-services --cluster '<cluster>' --services '<service>' --query 'services[0].desiredCount' --output text)"
    printf '  every %ss until held=0, at most %ss:\n' "$INTERVAL" "$TIMEOUT"
  fi
  printf '  + %s\n' "$(shq "${RO[@]}" ecs describe-services --cluster "$CL" --services "$SV" --query 'services[0].[desiredCount,runningCount]' --output text)"
  printf '  + %s\n' "$(shq "${RO[@]}" ecs list-tasks --cluster "$CL" --service-name "$SV" --desired-status RUNNING --query 'taskArns[]' --output text)"
  printf '  + %s\n' "$(shq "${RO[@]}" logs describe-log-streams --log-group-name '<celery log group>' --order-by LastEventTime --descending --max-items 50 --query 'logStreams[].logStreamName' --output text)"
  printf '  + %s   (one per running task, the last %ss, paged)\n' "$(shq "${RO[@]}" logs get-log-events --log-group-name '<celery log group>' --log-stream-name '<stream of the task>' --start-time '<now-lookback>' --start-from-head --output json)" "$LOOKBACK"
  [ "$CMD" = read ] || printf '  then the desired count check above again, and row celery-drain held 0\n'
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
