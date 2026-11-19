#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../target.sh"
usage() { echo "usage: TARGET_ENV=tb-prod $(basename "$0") restart|stop|start|read [--dry-run | --apply]"; }
MODE=""
for a in "$@"; do
  case "$a" in
    restart|stop|start|read) [ -z "$MODE" ] || { usage >&2; exit 64; }; MODE=$a ;;
    --apply|--dry-run) ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $a" >&2; usage >&2; exit 64 ;;
  esac
done
[ -n "$MODE" ] || { usage >&2; exit 64; }
[ "$TARGET_ENV" = tb-prod ] || { echo "refusing: the legacy Stalwart nodes belong to tb-prod; TARGET_ENV=$TARGET_ENV has none" >&2; exit 64; }
load_cutover_env
. "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/../timing.sh"

TAG=postboot.stalwart.node_services
WAIT=${LEGACY_SSM_WAIT:-600}
POLL=${LEGACY_SSM_POLL:-5}
case $MODE in
  restart) CMD='systemctl restart thundermail' ;;
  stop) CMD='systemctl disable --now thundermail' ;;
  start) CMD='systemctl enable --now thundermail' ;;
  read) CMD='systemctl is-active thundermail; systemctl is-enabled thundermail; exit 0' ;;
esac
LOG=$EVIDENCE_DIR/legacy-stalwart-$MODE-$(mode)-$TS.log
exec > >(tee -a "$LOG") 2>&1
NEEDS=0
need_line() { printf 'NEED  %s\n' "$*"; NEEDS=$((NEEDS + 1)); }
halt() { printf 'STOP legacy-stalwart %s (%s): %s\n' "$MODE" "$(mode)" "$*"; printf 'evidence: %s\n' "$LOG"; exit 1; }
end_needs() {
  [ "$NEEDS" = 0 ] && return 0
  printf 'legacy-stalwart %s %s: %d NEED item(s) above\n' "$MODE" "$(mode)" "$NEEDS"
  printf 'evidence: %s\n' "$LOG"
  exit 2
}
q() { printf '%q ' "$@" | sed 's/ $//'; }

printf 'legacy-stalwart %s (%s): TARGET_ENV=%s profile=%s region=%s\n' "$MODE" "$(mode)" "$TARGET_ENV" "$LEGACY_AWS_PROFILE" "$AWS_REGION"
printf 'command on each node: %s\n' "$CMD"

for t in aws jq; do command -v "$t" >/dev/null 2>&1 || need_line "tool $t is not on PATH"; done
end_needs

[ -f "${ENV_FILE:-}" ] || need_line "no env file ${ENV_FILE:-}: run build-cutover-env.sh"
read -r -a IDS <<<"${LEGACY_STALWART_NODE_IDS:-}"
bad=""
for i in "${IDS[@]+"${IDS[@]}"}"; do [[ $i =~ ^i-[0-9a-f]{8,17}$ ]] || bad="$bad $i"; done
if [ "${#IDS[@]}" != "${LEGACY_NODE_COUNT:-6}" ] || [ -n "$bad" ] || [ "$(printf '%s\n' "${IDS[@]+"${IDS[@]}"}" | sort -u | grep -c .)" != "${#IDS[@]}" ]; then
  need_line "LEGACY_STALWART_NODE_IDS in ${ENV_FILE:-the env file} holds ${#IDS[@]} id(s)${bad:+, not instance ids:$bad}; it needs ${LEGACY_NODE_COUNT:-6} distinct ids: run build-cutover-env.sh --rebuild"
fi
end_needs

if aws_legacy sts get-caller-identity --query Arn --output text >/dev/null 2>&1; then
  echo "AWS session for $LEGACY_AWS_PROFILE: ok"
else
  need_line "no AWS session for $LEGACY_AWS_PROFILE: login first"
fi
end_needs

printf '  read: %s\n' "$(q aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" ec2 describe-instances --instance-ids "${IDS[@]}" --query "Reservations[].Instances[].[InstanceId,State.Name,Tags[?Key==\`Name\`]|[0].Value,Tags[?Key==\`$TAG\`]|[0].Value]" --output text)"
desc=$(aws_legacy ec2 describe-instances --instance-ids "${IDS[@]}" --query "Reservations[].Instances[].[InstanceId,State.Name,Tags[?Key==\`Name\`]|[0].Value,Tags[?Key==\`$TAG\`]|[0].Value]" --output text 2>&1) || { need_line "ec2 describe-instances failed: $(tail -1 <<<"$desc")"; end_needs; }
printf '  read: %s\n' "$(q aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" ssm describe-instance-information --filters "Key=InstanceIds,Values=$(IFS=,; echo "${IDS[*]}")" --query 'InstanceInformationList[].[InstanceId,PingStatus]' --output text)"
ping=$(aws_legacy ssm describe-instance-information --filters "Key=InstanceIds,Values=$(IFS=,; echo "${IDS[*]}")" --query 'InstanceInformationList[].[InstanceId,PingStatus]' --output text 2>&1) || { need_line "ssm describe-instance-information failed: $(tail -1 <<<"$ping")"; end_needs; }

declare -A NAME ROLE
TARGETS=()
for i in "${IDS[@]}"; do
  line=$(awk -F'\t' -v i="$i" '$1 == i' <<<"$desc")
  [ "$(grep -c . <<<"$line")" = 1 ] || { need_line "$i: ec2 describe-instances returned $(grep -c . <<<"$line") row(s) for it"; continue; }
  IFS=$'\t' read -r _ state name svc <<<"$line"
  case $name in $LEGACY_NODE_NAME_FILTER) ;; *) need_line "$i: Name tag '$name' does not match $LEGACY_NODE_NAME_FILTER"; continue ;; esac
  [ "$state" = running ] || need_line "$name $i: instance state $state, not running"
  [ -n "$svc" ] && [ "$svc" != None ] || { need_line "$name $i: no $TAG tag"; continue; }
  p=$(awk -F'\t' -v i="$i" '$1 == i {print $2}' <<<"$ping")
  [ "$p" = Online ] || need_line "$name $i: SSM PingStatus ${p:-absent}, not Online"
  NAME[$i]=$name
  ROLE[$i]=$([ "$svc" = management ] && echo management || echo mail)
  if [ "$MODE" != restart ] || [ "${ROLE[$i]}" = mail ]; then TARGETS+=("$i"); fi
done
end_needs
for i in "${IDS[@]}"; do printf '%s\n' "${NAME[$i]} $i ${ROLE[$i]}"; done | sort | while read -r n i r; do
  t=skip; for x in "${TARGETS[@]}"; do [ "$x" != "$i" ] || t=target; done
  printf 'node %s %s %s %s\n' "$n" "$i" "$r" "$t"
done
[ "${#TARGETS[@]}" -gt 0 ] || halt "no node carries mail ($TAG other than management)"

if [ "$MODE" = restart ] && [ -n "$(awk -F'\t' '$1 == "cut-ecs" || $1 == "start-legacy-stalwart" {v = ""} $1 == "stop-legacy-stalwart" {v = $0} END {print v}' "$TIMING_TSV" 2>/dev/null)" ]; then
  need_line "a stop-legacy-stalwart row follows the last cut-ecs row in $TIMING_TSV: restart would start the stopped nodes; start is the Rollback mode"
fi
if [ "$MODE" = stop ]; then
  qd=$(awk -F'\t' '$1 == "cut-ecs" {v = ""} $1 == "queue-drain" {v = $5} $1 == "stop-W4" {v = "stop"} END {print v}' "$TIMING_TSV" 2>/dev/null || true)
  if [ "$qd" = 0 ]; then echo "gate: the last queue-drain row of this cut reads 0 in $TIMING_TSV"
  else need_line "the last queue-drain row of this cut in $TIMING_TSV reads '${qd:-<none>}', not 0 (a stop-W4 row after it reads 'stop'): run window/w4-migrate.sh --phase read --apply until it passes (W 4.2)"; fi
fi

PARAMS=$(jq -cn --arg c "$CMD" '{commands: [$c], executionTimeout: ["300"]}')
SEND=(ssm send-command --document-name AWS-RunShellScript --instance-ids "${TARGETS[@]}" --max-concurrency 100% --max-errors 100% --timeout-seconds 60 --comment "thundermail-deploy legacy-stalwart.sh $MODE" --parameters "$PARAMS" --query Command.CommandId --output text)
if [ "$APPLY" != 1 ]; then
  printf '  + %s\n' "$(q aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" "${SEND[@]}")"
  printf '  + %s   (each of the %d node(s), every %s s, up to %s s)\n' "$(q aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" ssm get-command-invocation --command-id '<CommandId>' --instance-id '<node>' --output json)" "${#TARGETS[@]}" "$POLL" "$WAIT"
  [ "$MODE" != read ] || printf '  + append row stop-legacy-stalwart <sent> <read> nodes %d to %s, only when all %d nodes read inactive and disabled\n' "${#IDS[@]}" "$TIMING_TSV" "${#IDS[@]}"
  end_needs
  printf 'legacy-stalwart %s dry-run: done (%d node(s))\n' "$MODE" "${#TARGETS[@]}"
  exit 0
fi
end_needs

SENT=$(date -u +%FT%TZ)
if [ "$MODE" = start ]; then
  printf 'start-legacy-stalwart\t%s\t%s\tnodes\t%d\n' "$SENT" "$SENT" "${#TARGETS[@]}" >> "$TIMING_TSV"
  echo "start-legacy-stalwart row written to $TIMING_TSV before the send: an earlier stop-legacy-stalwart row no longer counts"
fi
printf '  + %s\n' "$(q aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" "${SEND[@]}")"
cid=$(aws_legacy "${SEND[@]}" 2>&1) || halt "ssm send-command failed: $(tail -1 <<<"$cid")"
[[ $cid =~ ^[0-9a-f-]{36}$ ]] || halt "ssm send-command returned no command id: $(tail -1 <<<"$cid")"
echo "command id $cid, sent $SENT"
printf '  + %s   (each of the %d node(s), every %s s, up to %s s)\n' "$(q aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" ssm get-command-invocation --command-id "$cid" --instance-id '<node>' --output json)" "${#TARGETS[@]}" "$POLL" "$WAIT"

declare -A ST RC OUT ERR
deadline=$(( $(date +%s) + WAIT ))
while :; do
  open=0
  for i in "${TARGETS[@]}"; do
    case "${ST[$i]:-}" in Success|Failed|Cancelled|TimedOut) continue ;; esac
    if inv=$(aws_legacy ssm get-command-invocation --command-id "$cid" --instance-id "$i" --output json 2>/dev/null); then
      ST[$i]=$(jq -r '.Status // empty' <<<"$inv" 2>/dev/null || true)
      RC[$i]=$(jq -r '.ResponseCode // empty' <<<"$inv" 2>/dev/null || true)
      OUT[$i]=$(jq -r '.StandardOutputContent // ""' <<<"$inv" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//' || true)
      ERR[$i]=$(jq -r '.StandardErrorContent // ""' <<<"$inv" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//' || true)
    fi
    case "${ST[$i]:-}" in Success|Failed|Cancelled|TimedOut) ;; *) open=$((open + 1)) ;; esac
  done
  [ "$open" -gt 0 ] && [ "$(date +%s)" -lt "$deadline" ] || break
  sleep "$POLL"
done

LINES=$(for i in "${TARGETS[@]}"; do
  s=${ST[$i]:-no-answer}
  case $s in Pending|InProgress|Delayed|Cancelling) s="no-answer($s)" ;; esac
  printf 'node %s %s %s rc=%s %s\n' "${NAME[$i]}" "$i" "$s" "${RC[$i]:--}" "${OUT[$i]:-}"
done | sort)
printf '%s\n' "$LINES" | sed 's/ *$//'
for i in "${TARGETS[@]}"; do [ -z "${ERR[$i]:-}" ] || printf 'stderr %s %s: %s\n' "${NAME[$i]}" "$i" "${ERR[$i]}"; done
fails=$(grep -vc '^node [^ ]* [^ ]* Success ' <<<"$LINES" || true)
[ "$fails" = 0 ] || halt "$fails of ${#TARGETS[@]} node(s) failed or did not answer within $WAIT s (command id $cid)"

if [ "$MODE" = read ]; then
  DONE=$(date -u +%FT%TZ)
  bad=$(awk '$5 != "rc=0" || $6 != "inactive" || $7 != "disabled" || NF != 7' <<<"$LINES" | grep -c . || true)
  printf 'summary:'; awk '{print $6 "/" $7}' <<<"$LINES" | sort | uniq -c | awk '{printf " %s=%s", $2, $1}'; printf '\n'
  if [ "$bad" = 0 ] && [ "${#TARGETS[@]}" = "${#IDS[@]}" ]; then
    printf 'stop-legacy-stalwart\t%s\t%s\tnodes\t%d\n' "$SENT" "$DONE" "${#IDS[@]}" >> "$TIMING_TSV"
    echo "all ${#IDS[@]} nodes inactive and disabled: stop-legacy-stalwart row written to $TIMING_TSV"
  else
    echo "$bad of ${#IDS[@]} node(s) not inactive and disabled: no stop-legacy-stalwart row"
  fi
fi
printf 'legacy-stalwart %s apply: done (%d node(s))\n' "$MODE" "${#TARGETS[@]}"
