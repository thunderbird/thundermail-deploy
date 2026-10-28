#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
TARGETS=${*:-${LEGACY_ECS_TARGETS:-}}
[ -n "$TARGETS" ] || die "usage: snapshot-ecs.sh [cluster/service ...] (default: LEGACY_ECS_TARGETS)"
out=$EVIDENCE_DIR/ecs-before-$TS.json
services=$(for t in $TARGETS; do aws_legacy ecs describe-services --cluster "${t%%/*}" --services "${t##*/}" --query 'services[0].{cluster:clusterArn,serviceName:serviceName,desiredCount:desiredCount,runningCount:runningCount,status:status}' | jq --arg c "${t%%/*}" '. + {cluster: $c}'; done | jq -s '.')
ids=$(for t in $TARGETS; do printf 'service/%s ' "$t"; done)
scalable=$(aws_legacy application-autoscaling describe-scalable-targets --service-namespace ecs --resource-ids $ids --query 'ScalableTargets')
jq -n --argjson s "$services" --argjson a "$scalable" '{services: $s, scalable: $a}' > "$out"
jq -r '.services[] | "\(.cluster)/\(.serviceName) desired=\(.desiredCount) running=\(.runningCount)"' "$out"
jq -r '.scalable[] | "\(.ResourceId) min=\(.MinCapacity) max=\(.MaxCapacity) suspended=\(.SuspendedState.DynamicScalingInSuspended)"' "$out"
printf 'services=%s scalable_targets=%s\n%s\n' "$(jq '.services|length' "$out")" "$(jq '.scalable|length' "$out")" "$out"
