#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
USAGE="usage: hold25.sh set|read|queue-hold|release-port25|release-queue [--dry-run | --apply [--go 'operator words']]"
MODE=${POS[0]:-}
case "$MODE" in set|read|queue-hold|release-port25|release-queue) ;; *) echo "$USAGE" >&2; exit 64 ;; esac
[ "${#POS[@]}" = 1 ] || { echo "$USAGE" >&2; exit 64; }
[ "$TARGET_ENV" = tb-prod ] || { echo "hold25.sh holds tb-prod outbound mail only; TARGET_ENV=$TARGET_ENV" >&2; exit 64; }
need TARGET_STALWART_SG
SG=$TARGET_STALWART_SG
AWST=(aws --profile "$TARGET_AWS_PROFILE" --region "$AWS_REGION")
HOLD_HOURS=${QUEUE_HOLD_HOURS:-24}
HELD_MIN_HOURS=${QUEUE_HELD_MIN_HOURS:-12}
DOMAIN_SECS=${QUEUE_DOMAIN_ATTEMPT_SECONDS:-1360}
DRAIN_POLL=${QUEUE_DRAIN_POLL_SECONDS:-15}
DRAIN_MAX=${QUEUE_DRAIN_MAX_SECONDS:-5400}
OPEN_KEY='-1|-1|-1|0.0.0.0/0'
CLOSED_KEYS='tcp|0|24|0.0.0.0/0 tcp|26|65535|0.0.0.0/0 udp|0|65535|0.0.0.0/0 icmp|-1|-1|0.0.0.0/0'
RULES='[]'
ACCOUNT=""
Q='{}'

target_ok() {
  local vpc sgvpc sgp mode eni
  vpc=$("${AWST[@]}" eks describe-cluster --name "$TARGET_CLUSTER" --query cluster.resourcesVpcConfig.vpcId --output text) || return 1
  sgvpc=$("${AWST[@]}" ec2 describe-security-groups --group-ids "$SG" --query 'SecurityGroups[0].VpcId' --output text) || return 1
  [ -n "$vpc" ] && [ "$vpc" = "$sgvpc" ] || { echo "$SG is in VPC $sgvpc, cluster $TARGET_CLUSTER is in VPC $vpc"; return 1; }
  sgp=$("${KS[@]}" -n thundermail get securitygrouppolicy stalwart-server -o json | jq -r '.spec.securityGroups.groupIds | join(" ")') || return 1
  [ "$sgp" = "$SG" ] || { echo "SecurityGroupPolicy stalwart-server lists '$sgp', TARGET_STALWART_SG is $SG"; return 1; }
  mode=$("${KS[@]}" -n kube-system get ds aws-node -o json | jq -r '[.spec.template.spec.containers[] | select(.name == "aws-node") | .env[]? | select(.name == "POD_SECURITY_GROUP_ENFORCING_MODE" or .name == "ENABLE_POD_ENI") | "\(.name)=\(.value)"] | join(" ")') || return 1
  eni=$(grep -o 'ENABLE_POD_ENI=[^ ]*' <<<"$mode" || true)
  [ "$eni" = ENABLE_POD_ENI=true ] || { echo "aws-node ${eni:-ENABLE_POD_ENI unset}: SecurityGroupPolicy stalwart-server has no effect"; return 1; }
  mode=$(grep -o 'POD_SECURITY_GROUP_ENFORCING_MODE=[^ ]*' <<<"$mode" | cut -d= -f2 || true)
  [ "$mode" = strict ] || { echo "aws-node POD_SECURITY_GROUP_ENFORCING_MODE is '$mode', not strict: pod traffic may leave by the node group"; return 1; }
  echo "target: cluster $TARGET_CLUSTER, VPC $vpc; SecurityGroupPolicy stalwart-server -> $SG; pod security groups strict"
}

read_rules() {
  RULES=$("${AWST[@]}" ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG" --output json | jq -c '[.SecurityGroupRules[] | select(.IsEgress) | {id: .SecurityGroupRuleId, key: "\(.IpProtocol)|\(.FromPort // -1)|\(.ToPort // -1)|\(.CidrIpv4 // .CidrIpv6 // .ReferencedGroupInfo.GroupId // .PrefixListId // "")", p: .IpProtocol, f: (.FromPort // -1), t: (.ToPort // -1), d: (.Description // "")}]') || return 1
}

rule_id() { jq -r --arg k "$1" '[.[] | select(.key == $k) | .id][0] // empty' <<<"$RULES"; }
port25_open() { jq -e 'any(.[]; .p == "-1" or ((.p == "tcp" or .p == "6") and .f <= 25 and .t >= 25))' <<<"$RULES" >/dev/null; }

known_only() {
  local bad
  bad=$(jq -r --arg ok "$OPEN_KEY $CLOSED_KEYS" '($ok | split(" ")) as $k | .[] | select(.key as $x | $k | index($x) | not) | "\(.id) \(.key)"' <<<"$RULES")
  [ -z "$bad" ] || { echo "egress rules of $SG this tool did not write and will not touch:"; sed 's/^/  /' <<<"$bad"; return 1; }
}

show_port25() {
  local list
  list=$(jq -r '[.[] | "\(.p) \(if .f == -1 then "all" else "\(.f)-\(.t)" end) \(.key | split("|")[3])"] | join(", ")' <<<"$RULES")
  if port25_open; then echo "port25: open on $SG (egress: $list)"; else echo "port25: closed on $SG (egress: $list)"; fi
}

perms() {
  local k p f t c out='[]'
  for k in "$@"; do
    IFS='|' read -r p f t c <<<"$k"
    out=$(jq -c --arg p "$p" --argjson f "$f" --argjson t "$t" --arg c "$c" \
      '. + [{IpProtocol: $p} + (if $p == "-1" then {} else {FromPort: $f, ToPort: $t} end) + {IpRanges: [{CidrIp: $c, Description: (if $p == "-1" then "Allow all egress" else "outbound hold: all but TCP 25" end)}]}]' <<<"$out")
  done
  printf '%s' "$out"
}

snapshot_sg() {
  local f=$EVIDENCE_DIR/hold25-sg-$MODE$SUF-$TS.json
  jq -n --arg sg "$SG" --argjson r "$RULES" '{group: $sg, egress: $r}' > "$f"
  echo "egress rules before: $f"
}

swap() {
  local want=$1 drop=$2 k missing=() ids=() id
  read_rules || return 1
  known_only || return 1
  snapshot_sg
  show_port25
  for k in $want; do [ -n "$(rule_id "$k")" ] || missing+=("$k"); done
  for k in $drop; do id=$(rule_id "$k"); [ -z "$id" ] || ids+=("$id"); done
  if [ "${#missing[@]}" -gt 0 ]; then
    run "${AWST[@]}" ec2 authorize-security-group-egress --group-id "$SG" --ip-permissions "$(perms "${missing[@]}")" --query Return --output text || return 1
  fi
  if [ "${#ids[@]}" -gt 0 ]; then
    run "${AWST[@]}" ec2 revoke-security-group-egress --group-id "$SG" --security-group-rule-ids "${ids[@]}" --query Return --output text || return 1
  fi
  [ "${#missing[@]}" -gt 0 ] || [ "${#ids[@]}" -gt 0 ] || echo "nothing to write: $SG already holds exactly the rules this mode writes"
  [ "$APPLY" = 1 ] || { echo "read-back after --apply: describe-security-group-rules on $SG"; return 0; }
  read_rules || return 1
  printf 'read back: '
  show_port25
  for k in $want; do [ -n "$(rule_id "$k")" ] || { echo "read back: rule $k missing on $SG"; return 1; }; done
  for k in $drop; do [ -z "$(rule_id "$k")" ] || { echo "read back: rule $k still on $SG"; return 1; }; done
}

close_25() { swap "$CLOSED_KEYS" "$OPEN_KEY" && { [ "$APPLY" = 0 ] || ! port25_open; }; }
open_25() { swap "$OPEN_KEY" "$CLOSED_KEYS" && { [ "$APPLY" = 0 ] || port25_open; }; }

jmap() { curl -sS -m 60 -K <(curl_user "$STALWART_USER" "$STALWART_PASSWORD") -H 'Content-Type: application/json' --data "$1" "$STALWART_URL/jmap"; }
call() {
  jmap "$(jq -cn --arg a "$ACCOUNT" --arg m "$1" --argjson args "$2" '{using: ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"], methodCalls: [[$m, ({accountId: $a} + $args), "0"]]}')" \
    | jq -ce '.methodResponses[0] | if .[0] == "error" then error(.[1] | tostring) else .[1] end'
}

pods() {
  local j bad
  j=$("${KS[@]}" -n thundermail get pods -l app=stalwart -o json | jq -c '[.items[] | select(.metadata.deletionTimestamp == null) | {name: .metadata.name, phase: (.status.phase // "Unknown"), eni: ((.metadata.annotations // {})["vpc.amazonaws.com/pod-eni"] // "")}]') || return 1
  RUNNING=$(jq '[.[] | select(.phase == "Running")] | length' <<<"$j")
  echo "stalwart pods: $(jq length <<<"$j") (Running $RUNNING)"
  bad=$(jq -r '.[] | select(.phase == "Running" and .eni == "") | .name' <<<"$j")
  [ -z "$bad" ] || { echo "pods without a branch ENI, so $SG does not apply to them:"; sed 's/^/  /' <<<"$bad"; return 1; }
}

hold_state() {
  local h
  h=$(awk -F'\t' '$1 == "cut-ecs" || $1 == "hold25-release-queue" {h = ""} $1 == "hold25-queue-hold" {h = $2} END {print h}' "$TIMING_TSV" 2>/dev/null || true)
  if [ -n "$h" ]; then echo "queue hold: on since $h"; else echo "queue hold: off"; fi
}

connect() {
  local hp
  need STALWART_URL STALWART_USER STALWART_PASSWORD
  hp=${STALWART_URL#*://}
  hp=${hp%%/*}
  case "${hp%:*}" in 127.0.0.1|localhost|'[::1]') ;; *) echo "STALWART_URL host ${hp%:*} is not loopback: credentials not sent"; return 1 ;; esac
  port_forward || return 1
  ACCOUNT=$(curl -s -m 15 -K <(curl_user "$STALWART_USER" "$STALWART_PASSWORD") "$STALWART_URL/jmap/session" | jq -re '.primaryAccounts["urn:stalwart:jmap"]') || { echo "no urn:stalwart:jmap account in the session of $STALWART_URL"; return 1; }
}

read_queue() {
  local qids='[]' page list='[]' i now min cut=${1:-9999999999}
  while :; do
    page=$(call x:QueuedMessage/query "$(jq -cn --argjson p "$(jq length <<<"$qids")" '{position: $p, limit: 500}')" | jq -c '.ids') || return 1
    [ "$(jq length <<<"$page")" -gt 0 ] || break
    qids=$(jq -c --argjson p "$page" '. + $p' <<<"$qids")
    [ "$(jq length <<<"$page")" = 500 ] || break
  done
  for ((i = 0; i < $(jq length <<<"$qids"); i += 100)); do
    page=$(call x:QueuedMessage/get "$(jq -c --argjson i "$i" '{ids: .[$i:$i + 100]}' <<<"$qids")" | jq -c '.list') || return 1
    list=$(jq -c --argjson l "$page" '. + $l' <<<"$list")
  done
  now=$(date -u +%s)
  min=$((now + HELD_MIN_HOURS * 3600))
  Q=$(jq -c --argjson min "$min" --argjson now "$((now + 60))" --argjson cut "$cut" --argjson hold "$((HOLD_HOURS * 3600))" --argjson dom "$DOMAIN_SECS" '
    def due: sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601;
    map({id, created: ((.createdAt // "1970-01-01T00:00:00Z") | due), pend: [(.recipients // {}) | (if type == "array" then .[] else to_entries[] | .value + {address: .key} end) | select(.status["@type"] == "Scheduled" or .status["@type"] == "TemporaryFailure")]})
    | map(. + {remote: any(.pend[]; .queueName != "local"), held: ((.pend | length) > 0 and all(.pend[]; (.retryDue | due) >= $min)), later: ((.pend | length) > 0 and all(.pend[]; (.retryDue | due) > $now)), wrote: (if (.pend | length) > 0 then ([.pend[].retryDue | due] | min) - $hold else 0 end), domains: ([.pend[] | select(.queueName != "local") | (.address // "") | ascii_downcase | split("@") | last] | unique | length)})
    | {total: length, remote: [.[] | select(.remote) | .id], held: [.[] | select(.remote and .held) | .id], not_held: [.[] | select(.remote and (.held | not)) | .id], hold: [.[] | select(.remote and (.held | not) and .created < $cut) | .id], later: [.[] | select(.remote and .later) | .id], msgs: [.[] | select(.remote) | {id, created, held, wrote, bound: ((if .domains > 1 then .domains else 1 end) * $dom)}]}' <<<"$list") || return 1
}

show_queue() { jq -r '"queue: total=\(.total) remote=\(.remote | length) held=\(.held | length) not_held=\(.not_held | length)" + (if (.not_held | length) != (.hold | length) then " queued_after_release=\((.not_held | length) - (.hold | length))" else "" end)' <<<"$Q"; }

set_retry() {
  local qids=$1 at=$2 i n r bad
  n=$(jq length <<<"$qids")
  for ((i = 0; i < n; i += 50)); do
    printf '  + POST %s/jmap x:QueuedMessage/set nextRetry=%s ids=%s\n' "$STALWART_URL" "$at" "$(jq -r --argjson i "$i" '.[$i:$i + 50] | join(",")' <<<"$qids")"
    [ "$APPLY" = 1 ] || continue
    r=$(call x:QueuedMessage/set "$(jq -c --argjson i "$i" --arg at "$at" '{update: (.[$i:$i + 50] | map({key: ., value: {nextRetry: $at}}) | from_entries)}' <<<"$qids")") || return 1
    bad=$(jq -r '(.notUpdated // {}) | to_entries[] | select(.value.type != "notFound") | "\(.key) \(.value.type) \(.value.description // "")"' <<<"$r")
    [ -z "$bad" ] || { echo "not updated:"; sed 's/^/  /' <<<"$bad"; return 1; }
  done
  if [ "$APPLY" = 1 ]; then echo "wrote nextRetry=$at on $n message(s)"; else echo "would write nextRetry=$at on $n message(s)"; fi
}

hold_queue() {
  local until n
  read_queue "${1:-}" || return 1
  show_queue
  until=$(date -u -d "+$HOLD_HOURS hours" +%FT%TZ)
  set_retry "$(jq -c .hold <<<"$Q")" "$until" || return 1
  [ "$APPLY" = 1 ] || return 0
  read_queue "${1:-}" || return 1
  printf 'read back: '
  show_queue
  n=$(jq '.hold | length' <<<"$Q")
  VALUE=$(jq '.held | length' <<<"$Q")
  [ "$n" = 0 ] || { echo "read back: $n queued message(s) not held"; return 1; }
}

drain() {
  local start now st ran='[]' key last="" w
  start=$(date -u +%s)
  echo "drain: every remote message queued before $(date -u -d "@$start" +%FT%TZ) stays held past its longest delivery attempt ($DOMAIN_SECS s per remote domain) before port 25 opens"
  while :; do
    read_queue || return 1
    now=$(date -u +%s)
    st=$(jq -c --argjson now "$now" --argjson start "$start" --argjson ran "$ran" '
      [.msgs[] | select(.created < $start)] as $old
      | [$old[] | if .held then .wrote + .bound - $now else .bound end | select(. > 0)] as $w
      | {hold: [.msgs[] | select(.held | not) | .id], again: [.msgs[] | select((.held | not) and (.id | IN($ran[]))) | .id], old: ($old | length), late: ([.msgs[] | select(.created >= $start)] | length), waiting: ($w | length), longest: ($w | max // 0)}' <<<"$Q") || return 1
    key=$(jq -r '"\(.old) \(.waiting) \(.late)"' <<<"$st")
    [ "$key" = "$last" ] || jq -r '"drain: queued before the drain: \(.old) remote, \(.waiting) still waiting (longest \(.longest) s); queued since: \(.late)"' <<<"$st"
    last=$key
    ran=$(jq -c --argjson q "$Q" '. + [$q.msgs[] | select(.held) | .id] | unique' <<<"$ran")
    [ "$(jq '.again | length' <<<"$st")" = 0 ] || echo "drain: held again, overwritten by a delivery attempt that ended: ids=$(jq -r '.again | join(",")' <<<"$st")"
    if [ "$(jq '.hold | length' <<<"$st")" -gt 0 ]; then
      set_retry "$(jq -c .hold <<<"$st")" "$(date -u -d "+$HOLD_HOURS hours" +%FT%TZ)" || return 1
      ran=$(jq -c --argjson h "$(jq -c .hold <<<"$st")" '. + $h | unique' <<<"$ran")
    fi
    w=$(jq .longest <<<"$st")
    if [ "$APPLY" = 0 ]; then
      echo "drain: --apply would wait up to $w s, read the queue every $DRAIN_POLL s, hold again what an ending attempt overwrites, and stop after $DRAIN_MAX s"
      return 0
    fi
    if [ "$w" = 0 ]; then
      echo "drain: done after $((now - start)) s: no delivery attempt that started before its hold is still running for mail queued before the drain"
      echo "drain: unproven=$(jq .late <<<"$st") (queued during the drain and held, but an attempt that started before its hold can still deliver once port 25 opens)"
      return 0
    fi
    [ $((now - start)) -lt "$DRAIN_MAX" ] || { echo "drain: $(jq .waiting <<<"$st") message(s) not proven held after $DRAIN_MAX s; port 25 stays closed"; return 1; }
    sleep "$((w < DRAIN_POLL ? w : DRAIN_POLL))"
  done
}

release_queue() {
  read_queue || return 1
  show_queue
  VALUE=$(jq '.later | length' <<<"$Q")
  set_retry "$(jq -c .later <<<"$Q")" "$(date -u +%FT%TZ)" || return 1
  [ "$APPLY" = 1 ] || return 0
  read_queue || return 1
  printf 'read back: '
  show_queue
  [ "$(jq '.held | length' <<<"$Q")" = 0 ] || { echo "read back: messages still held"; return 1; }
}

gate_row() {
  [ -n "$(after_cut "$1")" ] && return 0
  [ "$APPLY" = 1 ] && stop "no $1 row in $TIMING_TSV since the last cut-ecs row: $2"
  printf 'NEED  row %s: %s\n' "$1" "$2"
  NEEDS=$((NEEDS + 1))
}

with_queue() {
  if connect; then "$@" || stop "$1 failed"; return 0; fi
  [ "$APPLY" = 1 ] && stop "no Stalwart JMAP session through the port-forward"
  printf 'NEED  a Running Stalwart pod and its port-forward for the queue\n'
  NEEDS=$((NEEDS + 1))
}

printf '== hold25 %s (%s)\n' "$MODE" "$(mode)"
target_ok || stop "target check failed"
case "$MODE" in
  read)
    read_rules || stop "describe-security-group-rules failed"
    show_port25
    pods || stop "a Running Stalwart pod is outside $SG"
    hold_state
    if [ "$RUNNING" -gt 0 ]; then connect || stop "no Stalwart JMAP session"; read_queue || stop "queue read failed"; show_queue
    else echo "queue: not read (no Running Stalwart pod)"; fi ;;
  set)
    pods || stop "a Running Stalwart pod is outside $SG"
    printf 'bash %s release-port25 --dry-run\nbash %s release-port25 --apply --go "<operator words>"\n' "$0" "$0" > "$RESTORE"
    close_25 || stop "closing port 25 on $SG failed"
    [ "$APPLY" = 0 ] || mark hold25-set sg "$SG" ;;
  queue-hold)
    with_queue hold_queue
    [ "$APPLY" = 0 ] || { mark hold25-queue-hold held "$VALUE"; hold_state; } ;;
  release-port25)
    gate_row hold25-queue-hold "the queue hold of 5.1 runs first"
    go_gate
    pods || stop "a Running Stalwart pod is outside $SG"
    with_queue drain
    printf 'bash %s set --dry-run\nbash %s set --apply\n' "$0" "$0" > "$RESTORE"
    open_25 || stop "opening port 25 on $SG failed"
    OPEN_AT=$(date -u +%s)
    with_queue hold_queue "$OPEN_AT"
    [ "$APPLY" = 0 ] || mark hold25-release-port25 held "$VALUE" ;;
  release-queue)
    gate_row hold25-release-port25 "port 25 is released first (7.5)"
    go_gate
    with_queue release_queue
    [ "$APPLY" = 0 ] || mark hold25-release-queue released "$VALUE" ;;
esac
finish
