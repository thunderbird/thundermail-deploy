#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
case ${TARGET_ENV:-tb-prod} in tb-prod) ;; *) { echo "celery-drain.sh reads the legacy Accounts Celery of the tb-prod cutover; TARGET_ENV=${TARGET_ENV:-} has none" >&2; exit 64; } ;; esac
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
CMD=${POS[0]:-}
case "$CMD:${#POS[@]}" in
  read:1|wait:1|timed:1) ;;
  *) echo "usage: celery-drain.sh read|wait|timed [--dry-run | --apply]" >&2; exit 64 ;;
esac
need LEGACY_ECS_TARGETS LEGACY_ECS_CLUSTER_PREFIX
RO=(aws --profile mzla-legacy-ro --region "$AWS_REGION")
TIMEOUT=${CELERY_DRAIN_TIMEOUT:-900}
INTERVAL=${CELERY_DRAIN_INTERVAL:-30}
FRESH=${CELERY_DRAIN_FRESH:-180}
READ_FOR=${CELERY_DRAIN_READ_FOR:-180}
for v in "$TIMEOUT" "$INTERVAL" "$FRESH" "$READ_FOR"; do [[ $v =~ ^[0-9]+$ ]] || { echo "CELERY_DRAIN_TIMEOUT, _INTERVAL, _FRESH and _READ_FOR take whole seconds (got '$v')" >&2; exit 64; }; done
HELD_RAW="" HELD_TSV=$EVIDENCE_DIR/celery-held-$TS.tsv
IGNORE=${CELERY_DRAIN_IGNORE:-}
[[ -z $IGNORE || $IGNORE =~ ^[0-9a-f-]+(,[0-9a-f-]+)*$ ]] || { echo "CELERY_DRAIN_IGNORE takes Celery task ids separated by commas (got '$IGNORE')" >&2; exit 64; }
W2_RESTORE=$EVIDENCE_DIR/cut-legacy-restore.txt
REVISIONS=$EVIDENCE_DIR/ecs-revisions-before.txt
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

declare -A TOK=() READ_END=()
SEEN="" DEADLINE=""

ro() {
  local left
  [ -n "$DEADLINE" ] || { "${RO[@]}" "$@"; return; }
  left=$(( DEADLINE - $(date +%s) ))
  [ "$left" -gt 0 ] || return 124
  timeout "$left" "${RO[@]}" "$@"
}

call_failed() {
  if [ "$1" = 124 ]; then echo "$2 did not answer within the ${READ_FOR}s of the timed read"; return 2; fi
  echo "$2 failed"
  return 1
}

events() {
  local s=$1 tok=${TOK[$1]:-} out next lines rc
  while :; do
    out=$(ro logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "$s" --start-from-head ${tok:+--next-token "$tok"} --output json) || { rc=$?; [ "$rc" != 124 ] || return 2; return 1; }
    lines=$(printf '%s' "$out" | jq -r '.events[] | [(.timestamp | tostring)] + (.message | first(capture("Task (?<n>[A-Za-z0-9_.:]+)\\[(?<i>[0-9a-f-]+)\\] (?<w>[a-z]+)"), (capture("Discarding revoked task: (?<n>[A-Za-z0-9_.:]+)\\[(?<i>[0-9a-f-]+)\\]") | .w = "revoked"), (capture("Hard time limit \\([0-9.]+s\\) exceeded for (?<n>[A-Za-z0-9_.:]+)\\[(?<i>[0-9a-f-]+)\\]") | .w = "timeout"), {n: "-", i: "-", w: "-"}) | [.n, .i, .w]) | join(" ")') || return 1
    next=$(printf '%s' "$out" | jq -r '.nextForwardToken // ""') || return 1
    [ -z "$lines" ] || SEEN+=$lines$'\n'
    [ -z "$next" ] || TOK[$s]=$next
    [ -n "$next" ] && [ "$next" != "$tok" ] || { READ_END[$s]=1; return 0; }
    tok=$next
  done
}

held_count() {
  sort -s -n -k1,1 | awk -v now="$1" -v ignore=",$IGNORE," -v out="$HELD_RAW" '
    function age(t) { return t > now ? 0 : (now - t) / 1000 }
    $1 + 0 > newest { newest = $1 + 0 }
    $4 == "-" || NF != 4 { next }
    { n++; last[$3] = $4; name[$3] = $2; at[$3] = $1; if (!($3 in first)) first[$3] = $1; if ($4 == "received") r++; else if ($4 != "retry") f++ }
    END {
      for (id in last) if (last[id] == "received" || last[id] == "retry") {
        if (index(ignore, "," id ",")) { g++; note = "ignored"; printf "  ignored %s %s %ds id=%s (CELERY_DRAIN_IGNORE)\n", name[id], last[id], age(at[id]), id }
        else { h++; note = "held"; printf "  held %s %s %ds id=%s\n", name[id], last[id], age(at[id]), id }
        if (out != "") printf "%s\t%s\t%s\t%d\t%d\t%s\n", name[id], id, last[id], first[id] / 1000, at[id] / 1000, note > out
      }
      printf "%d %d %d %d %d %d\n", h, r, f, newest ? age(newest) : -1, g, n
    }'
}

celery_read() {
  local svc desired running tasks t s rc streams now sum held rec fin age ign lines missing="" failed="" partial=""
  svc=$(ro ecs describe-services --cluster "$CL" --services "$SV" --query 'services[0].[desiredCount,runningCount]' --output text) || { call_failed $? "ecs describe-services $CL/$SV"; return; }
  read -r desired running <<<"$svc"
  tasks=$(ro ecs list-tasks --cluster "$CL" --service-name "$SV" --desired-status RUNNING --query 'taskArns[]' --output text) || { call_failed $? "ecs list-tasks $CL/$SV"; return; }
  read -ra tasks <<<"$(tr '\t' ' ' <<<"$tasks")"
  streams=$(ro logs describe-log-streams --log-group-name "$LOG_GROUP" --order-by LastEventTime --descending --max-items 50 --query 'logStreams[].logStreamName' --output text) || { call_failed $? "logs describe-log-streams"; return; }
  for t in "${tasks[@]}"; do
    s=$(tr '\t' '\n' <<<"$streams" | grep -m1 -- "/${t##*/}\$" || :)
    [ -n "$s" ] || { missing="$missing ${t##*/}"; continue; }
    rc=0
    events "$s" || rc=$?
    case $rc in 0) ;; 2) CUT=1; [ -n "${READ_END[$s]:-}" ] || partial="$partial $s" ;; *) failed="$failed $s" ;; esac
  done
  [ -z "$missing" ] || { echo "no log stream in $LOG_GROUP for running Celery task(s):$missing"; return 1; }
  [ -z "$failed" ] || { echo "logs get-log-events failed for$failed"; return 1; }
  [ -z "$partial" ] || { echo "the log history of$partial was not read to its end within ${READ_FOR}s; raise CELERY_DRAIN_READ_FOR"; return 1; }
  now=$(( $(date +%s) * 1000 ))
  [ -z "$HELD_RAW" ] || : > "$HELD_RAW"
  sum=$(printf '%s' "$SEEN" | held_count "$now")
  sed '$d' <<<"$sum"
  read -r held rec fin age ign lines <<<"$(tail -1 <<<"$sum")"
  printf 'celery held=%s received=%s finished=%s newest_log_age=%ss desired=%s running=%s tasks=%s ignored=%s\n' "$held" "$rec" "$fin" "$age" "$desired" "$running" "${#tasks[@]}" "$ign"
  HELD=$held IGNORED=$ign LINES=$lines
  READ_OK=0
  [ "$desired" -gt 0 ] && [ "$running" = "$desired" ] && [ "${#tasks[@]}" = "$running" ] && [ "$lines" -gt 0 ] && [ "$age" -ge 0 ] && [ "$age" -le "$FRESH" ] && READ_OK=1
  [ "$READ_OK" = 1 ] || echo "  not a drained read: needs desired=running=tasks above 0, a task line, and a log line in the last ${FRESH}s"
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

revisions() {
  local t
  [ -s "$REVISIONS" ] || { echo "no $REVISIONS: run the ECS loop of 1.5 first"; return 1; }
  for t in $LEGACY_ECS_TARGETS; do
    awk -F'\t' -v s="${t##*/}" '$1 == s {f = 1} END {exit !f}' "$REVISIONS" || { echo "$REVISIONS names no service ${t##*/}: run the ECS loop of 1.5 again"; return 1; }
  done
  echo "ECS revisions of 1.5: $REVISIONS"
}

same_services() {
  local t want wantd have name td d moved="" bad=0
  for t in $LEGACY_ECS_TARGETS; do
    want=$(awk -F'\t' -v s="${t##*/}" '$1 == s {print $2; exit}' "$REVISIONS")
    wantd=$(jq -r --arg c "${t%%/*}" --arg s "${t##*/}" '[.services[] | select(.cluster == $c and .serviceName == $s) | .desiredCount][0] // "none"' "$ECS_BEFORE")
    have=$("${RO[@]}" ecs describe-services --cluster "${t%%/*}" --services "${t##*/}" --query 'services[0].[serviceName,taskDefinition,desiredCount]' --output text) || { echo "ecs describe-services $t failed"; return 1; }
    IFS=$'\t' read -r name td d <<<"$have"
    printf '  %s task=%s saved=%s desired=%s saved=%s\n' "$t" "${td##*/}" "${want##*/}" "$d" "$wantd"
    [ "$name" = "${t##*/}" ] && [ "$td" = "$want" ] || bad=1
    [ "$d" = "$wantd" ] || moved="$moved ${t##*/}"
  done
  [ "$bad" = 0 ] || { echo "a service name or task definition differs from 1.5 $REVISIONS"; return 1; }
  echo "service names and task definitions equal 1.5"
  if [ -n "$moved" ]; then echo "desired count differs from the 1.5 snapshot for$moved: autoscaling moves it; the timed read goes on"
  else echo "desired counts equal the 1.5 snapshot"; fi
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
  printf 'CELERY_DRAIN_IGNORE: %s\n' "${IGNORE:-none}"
  printf '  + %s\n' "$(shq "${RO[@]}" logs describe-log-groups --log-group-name-prefix "$LEGACY_ECS_CLUSTER_PREFIX" --query 'logGroups[].logGroupName' --output text)"
  [ "$CMD" != wait ] || printf '  + %s   (for each of LEGACY_ECS_TARGETS, against the 1.5 snapshot)\n' "$(shq "${RO[@]}" ecs describe-services --cluster '<cluster>' --services '<service>' --query 'services[0].desiredCount' --output text)"
  [ "$CMD" != timed ] || printf '  + %s   (for each of LEGACY_ECS_TARGETS: name and task definition against %s, a stop on a difference; desired count against the 1.5 snapshot, printed only)\n' "$(shq "${RO[@]}" ecs describe-services --cluster '<cluster>' --services '<service>' --query 'services[0].[serviceName,taskDefinition,desiredCount]' --output text)" "$REVISIONS"
  [ "$CMD" != wait ] || printf '  every %ss until held=0, at most %ss:\n' "$INTERVAL" "$TIMEOUT"
  [ "$CMD" != timed ] || printf '  every %ss for at most %ss, ending early at held=0 (a held task never stops the read; every read call ends at the limit, and a log history not read to its end by then is a failed read):\n' "$INTERVAL" "$READ_FOR"
  printf '  + %s\n' "$(shq "${RO[@]}" ecs describe-services --cluster "$CL" --services "$SV" --query 'services[0].[desiredCount,runningCount]' --output text)"
  printf '  + %s\n' "$(shq "${RO[@]}" ecs list-tasks --cluster "$CL" --service-name "$SV" --desired-status RUNNING --query 'taskArns[]' --output text)"
  printf '  + %s\n' "$(shq "${RO[@]}" logs describe-log-streams --log-group-name '<celery log group>' --order-by LastEventTime --descending --max-items 50 --query 'logStreams[].logStreamName' --output text)"
  printf '  + %s   (one per running task: its whole stream once, then the new events after the forward token)\n' "$(shq "${RO[@]}" logs get-log-events --log-group-name '<celery log group>' --log-stream-name '<stream of the task>' --start-from-head --output json)"
  [ "$CMD" != wait ] || printf '  then the desired count check above again, and row celery-drain held 0\n'
  [ "$CMD" != timed ] || printf '  then the service check above again, the tasks still held to %s, and row celery-read held <n>\n' "$HELD_TSV"
}

write_held() {
  local name id state first last note
  printf 'task\tid\tstate\tfirst_log_utc\tlast_log_utc\tnote\n' > "$HELD_TSV"
  while IFS=$'\t' read -r name id state first last note; do
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$id" "$state" "$(date -u -d "@$first" +%FT%TZ)" "$(date -u -d "@$last" +%FT%TZ)" "$note"
  done < <(sort -t $'\t' -k5,5n "$HELD_RAW") >> "$HELD_TSV"
  rm -f "$HELD_RAW"
}

plan
if [ "$CMD" != read ]; then
  if out=$(ecs_snapshot); then printf '%s\n' "$out"
  elif [ "$APPLY" = 1 ]; then stop "$out"
  else printf 'NEED  %s\n' "$out"; NEEDS=$((NEEDS + 1)); fi
fi
if [ "$CMD" = timed ]; then
  if rev=$(revisions); then printf '%s\n' "$rev"
  elif [ "$APPLY" = 1 ]; then stop "$rev"
  else printf 'NEED  %s\n' "$rev"; NEEDS=$((NEEDS + 1)); fi
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
if [ "$CMD" = timed ]; then
  same_services || stop "service names or task definitions differ from 1.5 before the timed read"
  HELD_RAW=$EVIDENCE_DIR/celery-held-$TS.raw
  start=$(date +%s)
  end=$(( start + READ_FOR ))
  DEADLINE=$end
  rounds=0
  while :; do
    CUT=0 rc=0
    celery_read || rc=$?
    if [ "$rc" = 2 ] && [ "$rounds" -gt 0 ]; then echo "  the read at the limit did not finish; the last complete read above stands"; break; fi
    [ "$rc" = 0 ] || stop "celery read failed: a failed read is not an empty read"
    rounds=$((rounds + 1))
    [ "$LINES" -gt 0 ] || stop "no Celery task line in the running workers' log streams: a failed read is not an empty read"
    [ "$READ_OK" = 1 ] && [ "$HELD" = 0 ] && break
    left=$(( end - $(date +%s) ))
    [ "$CUT" = 0 ] || { echo "  the read stopped at the limit; tasks are counted from the events read by then"; break; }
    [ "$left" -gt 0 ] || break
    sleep "$(( left < INTERVAL ? left : INTERVAL ))"
    [ "$(date +%s)" -lt "$end" ] || break
  done
  same_services || stop "service names or task definitions differ from 1.5 after the timed read"
  write_held
  mark celery-read held "$HELD"
  [ "$READ_OK" = 1 ] || echo "  note: the last read is not a drained read (see above); its tasks are recorded all the same"
  printf 'celery timed read: held=%s ignored=%s after %ss of at most %ss; recorded in %s\n' "$HELD" "$IGNORED" "$(( $(date +%s) - start ))" "$READ_FOR" "$HELD_TSV"
  finish
  exit 0
fi
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
