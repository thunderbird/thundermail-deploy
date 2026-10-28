#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need LEGACY_PUB_SG LEGACY_PUB_SG_RULE_IDS LEGACY_PUB_LB_ARN LEGACY_ECS_TARGETS LEGACY_ASG_RESOURCE_IDS OLD_STALWART_URL OLD_STALWART_USER OLD_STALWART_PASSWORD
RESTORE=$EVIDENCE_DIR/cut-legacy-restore.txt
AWSL=(aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION")
MAIL_HOST=mail.stage-thundermail.com
WEB_HOSTS="accounts-stage.tb.pro auth-stage.tb.pro"
read -ra RULES <<<"$LEGACY_PUB_SG_RULE_IDS"

snap_sg() {
  local out id
  out=$(bash "$MIG_DIR/snapshot-legacy-sg.sh") || return 1
  printf '%s\n' "$out"
  SG_SNAP=$(tail -1 <<<"$out")
  for id in "${RULES[@]}"; do
    jq -e --arg id "$id" 'any(.rules[]; .SecurityGroupRuleId == $id and .IsEgress == false)' "$SG_SNAP" >/dev/null || { echo "rule $id is not an ingress rule of $LEGACY_PUB_SG"; return 1; }
  done
  jq -r '.rules[] | select(.IsEgress == false) | "  \(.SecurityGroupRuleId) \(.IpProtocol) \(.FromPort)-\(.ToPort) \(.CidrIpv4 // .CidrIpv6 // .ReferencedGroupInfo.GroupId // .PrefixListId)"' "$SG_SNAP"
  VALUE=$(jq '[.rules[] | select(.IsEgress == false)] | length' "$SG_SNAP")
  [ "$VALUE" = "${#RULES[@]}" ] || { echo "group has $VALUE ingress rules, LEGACY_PUB_SG_RULE_IDS has ${#RULES[@]}"; return 1; }
  echo "rule ids: ${#RULES[@]} of ${#RULES[@]} present"
}

snap_ecs() {
  local out want
  out=$(bash "$MIG_DIR/snapshot-ecs.sh") || return 1
  printf '%s\n' "$out"
  ECS_BEFORE=$(tail -1 <<<"$out")
  VALUE=$(jq '[.services[] | select(.status == "ACTIVE")] | length' "$ECS_BEFORE")
  want=$(wc -w <<<"$LEGACY_ECS_TARGETS")
  [ "$VALUE" = "$want" ] || { echo "ACTIVE services=$VALUE, LEGACY_ECS_TARGETS has $want"; return 1; }
  want=$(wc -w <<<"$LEGACY_ASG_RESOURCE_IDS")
  [ "$(jq '.scalable | length' "$ECS_BEFORE")" = "$want" ] || { echo "scalable targets differ from LEGACY_ASG_RESOURCE_IDS ($want)"; return 1; }
  echo "services=$VALUE clusters=$(jq '[.services[].cluster] | unique | length' "$ECS_BEFORE") scalable_targets=$want"
}

stage_restore() {
  local f
  printf 'bash %s %s %s\n' \
    "$MIG_DIR/restore-legacy-sg.sh" "$SG_SNAP" --dry-run \
    "$MIG_DIR/restore-legacy-sg.sh" "$SG_SNAP" --apply \
    "$MIG_DIR/restore-ecs.sh" "$ECS_BEFORE" --dry-run \
    "$MIG_DIR/restore-ecs.sh" "$ECS_BEFORE" --apply > "$RESTORE"
  bash "$MIG_DIR/restore-legacy-sg.sh" "$SG_SNAP" --dry-run | tail -1 || return 1
  bash "$MIG_DIR/restore-ecs.sh" "$ECS_BEFORE" --dry-run || return 1
  for f in "$RESTORE" "$SG_SNAP" "$ECS_BEFORE"; do [ -s "$f" ] || { echo "$f is missing or empty"; return 1; }; done
  VALUE=3
  printf 'present: %s %s %s\n' "$RESTORE" "$SG_SNAP" "$ECS_BEFORE"
}

mgmt() {
  local body
  body=$(curl -sk -m 15 -K <(curl_user "$OLD_STALWART_USER" "$OLD_STALWART_PASSWORD") "$OLD_STALWART_URL/api/principal?types=domain") || { echo "mgmt API on $OLD_STALWART_URL did not answer"; return 1; }
  jq -e '.data' <<<"$body" >/dev/null 2>&1 || { echo "mgmt API on $OLD_STALWART_URL answered without data"; return 1; }
  echo "mgmt API answers on $OLD_STALWART_URL (domains=$(jq -r '.data.total // (.data.items | length)' <<<"$body"))"
}

port_open() {
  if [ "$2" = 993 ]; then
    timeout 10 openssl s_client -connect "$1:993" -servername "$MAIL_HOST" </dev/null >/dev/null 2>&1
  else
    timeout 10 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
  fi
}

probe_legacy() {
  local dns ip port open=0
  dns=$("${AWSL[@]}" elbv2 describe-load-balancers --load-balancer-arns "$LEGACY_PUB_LB_ARN" --query 'LoadBalancers[0].DNSName' --output text) || return 1
  ip=$(doh "$dns")
  [ -n "$ip" ] || { echo "no A record for $dns via DoH"; return 1; }
  for port in 993 25 443; do
    if port_open "$ip" "$port"; then echo "  $dns ($ip):$port open"; open=$((open + 1)); else echo "  $dns ($ip):$port closed"; fi
  done
  VALUE=$open
}

cut_stalwart() {
  run "${AWSL[@]}" ec2 revoke-security-group-ingress --group-id "$LEGACY_PUB_SG" --security-group-rule-ids "${RULES[@]}" --query Return --output text || return 1
  probe_legacy || return 1
  if [ "$APPLY" = 1 ] && [ "$VALUE" != 0 ]; then echo "$VALUE legacy port(s) still open after the revoke"; return 1; fi
  mgmt
}

running() {
  local t
  for t in $LEGACY_ECS_TARGETS; do
    "${AWSL[@]}" ecs describe-services --cluster "${t%%/*}" --services "${t##*/}" --query 'services[0].[serviceName,desiredCount,runningCount]' --output text || return 1
  done
}

drained() { running | awk '{n += $3} END {exit !(NR > 0 && n == 0)}'; }

cut_ecs() {
  local t max start
  for t in $LEGACY_ECS_TARGETS; do
    max=$(jq -r --arg r "service/$t" '.scalable[] | select(.ResourceId == $r) | .MaxCapacity' "$ECS_BEFORE")
    if [ -n "$max" ]; then
      run "${AWSL[@]}" application-autoscaling register-scalable-target --service-namespace ecs --resource-id "service/$t" --scalable-dimension ecs:service:DesiredCount --min-capacity 0 --max-capacity "$max" --suspended-state DynamicScalingInSuspended=true,DynamicScalingOutSuspended=true,ScheduledScalingSuspended=true || return 1
    fi
    run "${AWSL[@]}" ecs update-service --cluster "${t%%/*}" --service "${t##*/}" --desired-count 0 --query 'service.[serviceName,desiredCount]' --output text || return 1
  done
  start=$(date +%s)
  if [ "$APPLY" = 1 ]; then until_ok 900 drained || { running; echo "tasks still running after 900s"; return 1; }; fi
  echo "  service desired running:"
  running | sed 's/^/  /' || return 1
  VALUE=$(( $(date +%s) - start ))
}

check_503() {
  local h ip code bad=0
  for h in $WEB_HOSTS; do
    ip=$(doh "$h")
    code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' --resolve "$h:443:${ip:-0.0.0.0}" "https://$h/" || true)
    echo "  https://$h/ $code"
    [ "$code" = 503 ] || bad=$((bad + 1))
  done
  VALUE=$bad
  [ "$APPLY" = 0 ] || [ "$bad" = 0 ]
}

prereqs
step snap-legacy-sg rules snap_sg
step snap-ecs services snap_ecs
step stage-restore files stage_restore
mgmt || stop "mgmt tunnel does not answer before the cut"
go_gate
step cut-stalwart open_ports cut_stalwart
step cut-ecs drain_seconds cut_ecs
setenv CUT_ECS_END "$(tail -1 "$TIMING_TSV" | cut -f3)"
if [ "$APPLY" = 1 ]; then
  step check-503 not_503 until_ok 180 check_503
else
  step check-503 not_503 check_503
fi
mgmt || stop "mgmt tunnel stopped answering"
finish
