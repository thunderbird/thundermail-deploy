#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SNAP=${1:-}
[ -f "$SNAP" ] || die "usage: restore-ecs.sh ecs-before-<ts>.json [--apply]"
run() { printf '  aws --profile %s --region %s %s\n' "$LEGACY_AWS_PROFILE" "$AWS_REGION" "$*"; [ "$APPLY" = 1 ] || return 0; aws_legacy "$@" >/dev/null || fail=$((fail + 1)); }
fail=0
while IFS=$'\t' read -r cluster svc desired; do
  printf '%s/%s desiredCount=%s\n' "$cluster" "$svc" "$desired"
  run ecs update-service --cluster "$cluster" --service "$svc" --desired-count "$desired"
done < <(jq -r '.services[] | [.cluster, .serviceName, .desiredCount] | @tsv' "$SNAP")
while IFS=$'\t' read -r rid min max; do
  printf '%s min=%s max=%s\n' "$rid" "$min" "$max"
  run application-autoscaling register-scalable-target --service-namespace ecs --resource-id "$rid" --scalable-dimension ecs:service:DesiredCount --min-capacity "$min" --max-capacity "$max"
done < <(jq -r '.scalable[] | [.ResourceId, .MinCapacity, .MaxCapacity] | @tsv' "$SNAP")
while IFS=$'\t' read -r rid; do
  printf '%s unsuspend\n' "$rid"
  run application-autoscaling register-scalable-target --service-namespace ecs --resource-id "$rid" --scalable-dimension ecs:service:DesiredCount --suspended-state DynamicScalingInSuspended=false,DynamicScalingOutSuspended=false,ScheduledScalingSuspended=false
done < <(jq -r '.scalable[] | .ResourceId' "$SNAP")
if [ "$APPLY" = 1 ]; then
  while IFS=$'\t' read -r cluster svc; do
    aws_legacy ecs describe-services --cluster "$cluster" --services "$svc" --query 'services[0].[serviceName,desiredCount,runningCount]' --output text
  done < <(jq -r '.services[] | [.cluster, .serviceName] | @tsv' "$SNAP")
fi
printf 'mode=%s services=%s scalable_targets=%s failed=%s\n' "$(mode)" "$(jq '.services|length' "$SNAP")" "$(jq '.scalable|length' "$SNAP")" "$fail"
[ "$fail" = 0 ]
