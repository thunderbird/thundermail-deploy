#!/usr/bin/env bash
set -euo pipefail
MODE=${1:-}
[ "$MODE" != read ] || shift
. "$(dirname "${BASH_SOURCE[0]}")/../target.sh"
case "$MODE" in
  read) ;;
  stop|start|restart) echo "legacy-stalwart.sh $MODE: not in this tool (Tooling issue, ## Open, legacy Stalwart process commands); only read is built" >&2; exit 64 ;;
  *) echo "usage: TARGET_ENV=<env> legacy-stalwart.sh read [--dry-run | --apply]" >&2; exit 64 ;;
esac
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need LEGACY_STALWART_NODE_IDS LEGACY_AWS_PROFILE AWS_REGION
UNIT=thundermail
SSM_READ="systemctl is-active $UNIT; systemctl is-enabled $UNIT; true"
SSM_WAIT=${SSM_WAIT:-180}
read -ra NODES <<<"$LEGACY_STALWART_NODE_IDS"
AWSL=(aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION")

read_gate() {
  local out
  printf '  + bash %s stop-legacy\n' "$MIG_DIR/window/gate.sh"
  out=$(bash "$MIG_DIR/window/gate.sh" stop-legacy) && { echo "$out"; return 0; }
  echo "$out"
  [ "$APPLY" = 1 ] && return 1
  printf 'NEED  the read writes the stop-legacy-stalwart row only after the read phase and a queue-drain 0 row (dry run continues)\n'
  NEEDS=$((NEEDS + 1))
}

invocations() { "${AWSL[@]}" ssm list-command-invocations --command-id "$1" --details --output json; }
settled() {
  INV=$(invocations "$1") || return 1
  [ "$(jq '[.CommandInvocations[] | select(.Status | test("^(Success|Failed|Cancelled|TimedOut|Undeliverable|Terminated)$"))] | length' <<<"$INV")" = "${#NODES[@]}" ]
}

read_nodes() {
  local id cmd bad=0 n=0 active enabled
  [ "${#NODES[@]}" = "${LEGACY_NODE_COUNT:-6}" ] || { echo "LEGACY_STALWART_NODE_IDS holds ${#NODES[@]} ids, want ${LEGACY_NODE_COUNT:-6}"; return 1; }
  printf '  + %s ssm send-command --document-name AWS-RunShellScript --instance-ids %s --parameters commands=%s\n' "$(shq "${AWSL[@]}")" "${NODES[*]}" "$(shq "$SSM_READ")"
  printf '  + wait up to %ss for %s invocations, then one line per node: is-active, is-enabled\n' "$SSM_WAIT" "${#NODES[@]}"
  [ "$APPLY" = 1 ] || return 0
  cmd=$("${AWSL[@]}" ssm send-command --document-name AWS-RunShellScript --instance-ids "${NODES[@]}" --comment "read unit $UNIT (W 4.3)" \
    --parameters "$(jq -cn --arg c "$SSM_READ" '{commands: [$c]}')" --query Command.CommandId --output text) || { echo "ssm send-command failed"; return 1; }
  echo "ssm command $cmd"
  until_ok "$SSM_WAIT" settled "$cmd" || { echo "not all ${#NODES[@]} invocations of $cmd ended within ${SSM_WAIT}s"; return 1; }
  for id in "${NODES[@]}"; do
    active=$(jq -r --arg i "$id" '.CommandInvocations[] | select(.InstanceId == $i) | if .Status == "Success" then (.CommandPlugins[0].Output // "" | split("\n") | .[0]) else "status-" + .Status end' <<<"$INV")
    enabled=$(jq -r --arg i "$id" '.CommandInvocations[] | select(.InstanceId == $i) | if .Status == "Success" then (.CommandPlugins[0].Output // "" | split("\n") | .[1] // "") else "" end' <<<"$INV")
    printf 'node %s %s=%s enabled=%s\n' "$id" "$UNIT" "${active:-none}" "${enabled:-none}"
    case "$active:$enabled" in
      inactive:disabled|failed:disabled) n=$((n + 1)) ;;
      *) bad=1 ;;
    esac
  done
  VALUE=$n
  [ "$bad" = 0 ] || { echo "$n of ${#NODES[@]} nodes report $UNIT inactive and disabled: no stop-legacy-stalwart row"; return 1; }
  mark stop-legacy-stalwart nodes "$n"
  echo "legacy Stalwart stopped on all $n nodes: stop-legacy-stalwart row written to $TIMING_TSV"
}

step read-gate "" read_gate
step legacy-read nodes read_nodes
finish
