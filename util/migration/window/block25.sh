#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
USAGE="usage: block25.sh block|restore|read [--dry-run | --apply [--go 'operator words']]"
MODE=${POS[0]:-}
case "$MODE" in block|restore|read) ;; *) echo "$USAGE" >&2; exit 64 ;; esac
[ "${#POS[@]}" = 1 ] || { echo "$USAGE" >&2; exit 64; }
[ "$TARGET_ENV" = tb-prod ] || { echo "block25.sh blocks the legacy production mail nodes only; TARGET_ENV=$TARGET_ENV" >&2; exit 64; }
need LEGACY_STALWART_NODE_IDS
AWSL=(aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION")
OPEN_KEY='tcp|0|65535|0.0.0.0/0'
CLOSED_KEYS='tcp|0|24|0.0.0.0/0 tcp|26|65535|0.0.0.0/0'
GROUPS_JSON='{}'
SNAP=""

read_nodes() {
  local out n bad ids
  read -ra ids <<<"$LEGACY_STALWART_NODE_IDS"
  [ "${#ids[@]}" = "$LEGACY_NODE_COUNT" ] || { echo "LEGACY_STALWART_NODE_IDS holds ${#ids[@]} ids, the plan has $LEGACY_NODE_COUNT nodes"; return 1; }
  out=$("${AWSL[@]}" ec2 describe-instances --instance-ids "${ids[@]}" --output json | jq -c '[.Reservations[].Instances[] | {id: .InstanceId, name: ([.Tags[]? | select(.Key == "Name") | .Value][0] // ""), groups: ([.NetworkInterfaces[].Groups[].GroupId] | unique)}]') || return 1
  n=$(jq length <<<"$out")
  [ "$n" = "$LEGACY_NODE_COUNT" ] || { echo "describe-instances returned $n of $LEGACY_NODE_COUNT nodes"; return 1; }
  bad=""
  while IFS=$'\t' read -r name groups; do
    [[ $name == $LEGACY_NODE_NAME_FILTER ]] || bad="$bad name '$name' does not match $LEGACY_NODE_NAME_FILTER;"
    [ "$(wc -w <<<"$groups")" = 1 ] || bad="$bad $name has groups '$groups', not one;"
  done < <(jq -r '.[] | [.name, (.groups | join(" "))] | @tsv' <<<"$out")
  n=$(jq '[.[].groups[0]] | unique | length' <<<"$out")
  [ "$n" = "$LEGACY_NODE_COUNT" ] || bad="$bad the $LEGACY_NODE_COUNT nodes share groups ($n distinct);"
  [ -z "$bad" ] || { echo "refusing:$bad"; return 1; }
  NODES=$out
  jq -r '.[] | "node \(.name) \(.groups[0])"' <<<"$NODES" | sort
}

read_groups() {
  local g r o
  GROUPS_JSON='{}'
  for g in $(jq -r '[.[].groups[0]] | unique | .[]' <<<"$NODES"); do
    o=$("${AWSL[@]}" ec2 describe-network-interfaces --filters "Name=group-id,Values=$g" --output json | jq -r --argjson n "$NODES" '[$n[].id] as $ids | .NetworkInterfaces[] | select((.Attachment.InstanceId // "") as $i | $ids | index($i) | not) | .NetworkInterfaceId') || return 1
    [ -z "$o" ] || { echo "refusing: $g is also on network interfaces of other instances: $(tr '\n' ' ' <<<"$o")"; return 1; }
    r=$("${AWSL[@]}" ec2 describe-security-group-rules --filters "Name=group-id,Values=$g" --output json | jq -c '[.SecurityGroupRules[] | select(.IsEgress) | {id: .SecurityGroupRuleId, key: "\(.IpProtocol)|\(.FromPort // -1)|\(.ToPort // -1)|\(.CidrIpv4 // .CidrIpv6 // .ReferencedGroupInfo.GroupId // .PrefixListId // "")", p: .IpProtocol, f: (.FromPort // -1), t: (.ToPort // -1), d: (.Description // "")}]') || return 1
    GROUPS_JSON=$(jq -c --arg g "$g" --argjson r "$r" '. + {($g): $r}' <<<"$GROUPS_JSON")
  done
}

rule_id() { jq -r --arg g "$1" --arg k "$2" '[.[$g][] | select(.key == $k) | .id][0] // empty' <<<"$GROUPS_JSON"; }
open25() { jq -e --arg g "$1" 'any(.[$g][]; .p == "-1" or ((.p == "tcp" or .p == "6") and .f <= 25 and .t >= 25))' <<<"$GROUPS_JSON" >/dev/null; }

known_only() {
  local bad
  bad=$(jq -r --arg ok "$OPEN_KEY $CLOSED_KEYS" '($ok | split(" ")) as $k | to_entries[] | .key as $g | .value[] | select(.key as $x | $k | index($x) | not) | "\($g) \(.id) \(.key)"' <<<"$GROUPS_JSON")
  [ -z "$bad" ] || { echo "egress rules this tool did not write and will not touch:"; sed 's/^/  /' <<<"$bad"; return 1; }
}

show() {
  local g b=0 o=0
  for g in $(jq -r 'keys[]' <<<"$GROUPS_JSON"); do
    if open25 "$g"; then o=$((o + 1)); echo "  $g OPEN    $(jq -r --arg g "$g" '[.[$g][] | "\(.p) \(if .f == -1 then "all" else "\(.f)-\(.t)" end)"] | join(", ")' <<<"$GROUPS_JSON")"
    else b=$((b + 1)); echo "  $g BLOCKED $(jq -r --arg g "$g" '[.[$g][] | "\(.p) \(if .f == -1 then "all" else "\(.f)-\(.t)" end)"] | join(", ")' <<<"$GROUPS_JSON")"; fi
  done
  echo "legacy outbound: blocked=$b open=$o groups=$((b + o))"
  BLOCKED=$b
  OPENED=$o
}

perm() {
  local p f t c
  IFS='|' read -r p f t c <<<"$1"
  jq -c -n --arg p "$p" --argjson f "$f" --argjson t "$t" --arg c "$c" --arg d "$2" '[{IpProtocol: $p, FromPort: $f, ToPort: $t, IpRanges: [{CidrIp: $c, Description: $d}]}]'
}

open_desc() {
  local d=""
  [ -z "$SNAP" ] || d=$(jq -r --arg g "$1" --arg k "$OPEN_KEY" '[.groups[$g][]? | select(.key == $k) | .d][0] // empty' "$SNAP")
  printf '%s' "${d:-Allow traffic from the instance out to the Internet}"
}

swap() {
  local want=$1 drop=$2 g k id ids d n=0
  for g in $(jq -r 'keys[]' <<<"$GROUPS_JSON"); do
    for k in $want; do
      [ -z "$(rule_id "$g" "$k")" ] || continue
      if [ "$k" = "$OPEN_KEY" ]; then d=$(open_desc "$g"); else d="legacy outbound block: all but TCP 25"; fi
      run "${AWSL[@]}" ec2 authorize-security-group-egress --group-id "$g" --ip-permissions "$(perm "$k" "$d")" --query Return --output text || return 1
      n=$((n + 1))
    done
    ids=()
    for k in $drop; do id=$(rule_id "$g" "$k"); [ -z "$id" ] || ids+=("$id"); done
    [ "${#ids[@]}" = 0 ] || { run "${AWSL[@]}" ec2 revoke-security-group-egress --group-id "$g" --security-group-rule-ids "${ids[@]}" --query Return --output text || return 1; n=$((n + 1)); }
  done
  [ "$n" -gt 0 ] || echo "nothing to write: every node group already holds exactly the rules this mode writes"
}

snapshot() {
  SNAP=$EVIDENCE_DIR/legacy-node-sg-before$SUF-$TS.json
  jq -n --argjson n "$NODES" --argjson g "$GROUPS_JSON" '{nodes: $n, groups: $g}' > "$SNAP"
  echo "egress rules before: $SNAP"
}

legacy_queue() {
  local body
  legacy_tunnel || return 1
  body=$(curl -sk -m 15 -K <(curl_user "$OLD_STALWART_USER" "$OLD_STALWART_PASSWORD") "$OLD_STALWART_URL/api/queue/messages?limit=1") || { echo "legacy admin API on $OLD_STALWART_URL did not answer"; return 1; }
  LEGACY_QUEUE=$(jq -re '.data.total' <<<"$body") || { echo "legacy admin API answered without data.total"; return 1; }
  [[ $LEGACY_QUEUE =~ ^[0-9]+$ ]] || { echo "legacy admin API answered a data.total that is not a whole number: $LEGACY_QUEUE"; return 1; }
  echo "legacy queue total=$LEGACY_QUEUE (admin API through the tunnel)"
}

block_gates() {
  local miss=""
  [ -n "$(awk -F'\t' '$1 == "cut-ecs"' "$TIMING_TSV" 2>/dev/null)" ] || miss="row cut-ecs (2.2 of the window) is not in $TIMING_TSV"
  if [ -z "$miss" ]; then
    need OLD_STALWART_URL OLD_STALWART_USER OLD_STALWART_PASSWORD
    if ! legacy_queue; then miss="the legacy queue was not read"
    elif [ "$LEGACY_QUEUE" != 0 ] && { [[ ! ${LEGACY_QUEUE_ACCEPT:-} =~ ^[0-9]+$ ]] || [ "$LEGACY_QUEUE" -gt "$LEGACY_QUEUE_ACCEPT" ]; }; then miss="the legacy queue holds $LEGACY_QUEUE message(s): wait for 2.4 (or accept it: LEGACY_QUEUE_ACCEPT=<n> with n at least $LEGACY_QUEUE)"
    elif [ "$LEGACY_QUEUE" != 0 ]; then echo "legacy queue: $LEGACY_QUEUE message(s) stay queued on legacy and move with the data at 4.2 (LEGACY_QUEUE_ACCEPT=$LEGACY_QUEUE_ACCEPT)"; fi
  fi
  [ -n "$miss" ] || return 0
  [ "$APPLY" = 1 ] && stop "$miss"
  printf 'NEED  %s\n' "$miss"
  NEEDS=$((NEEDS + 1))
}

readback() {
  [ "$APPLY" = 1 ] || { echo "read-back after --apply: describe-security-group-rules on each node group"; return 0; }
  read_groups || return 1
  printf 'read back:\n'
  show
}

printf '== block25 %s (%s)\n' "$MODE" "$(mode)"
read_nodes || stop "node read failed"
read_groups || stop "describe-security-group-rules failed"
case "$MODE" in
  read)
    show ;;
  block)
    known_only || stop "unexpected egress rule on a node group"
    show
    snapshot
    printf 'bash %s restore --dry-run\nbash %s restore --apply\n' "$0" "$0" > "$RESTORE"
    block_gates
    go_gate
    swap "$CLOSED_KEYS" "$OPEN_KEY" || stop "block write failed"
    readback || stop "read-back failed"
    if [ "$APPLY" = 1 ]; then
      [ "$OPENED" = 0 ] || stop "$OPENED node group(s) still allow TCP 25"
      legacy_queue || stop "the legacy admin API stopped answering after the block"
      mark legacy-outbound-block groups "$BLOCKED"
    fi ;;
  restore)
    known_only || stop "unexpected egress rule on a node group"
    show
    SNAP=$(ls -1t "$EVIDENCE_DIR"/legacy-node-sg-before-*.json 2>/dev/null | head -1 || true)
    echo "descriptions from: ${SNAP:-none (default description)}"
    swap "$OPEN_KEY" "$CLOSED_KEYS" || stop "restore write failed"
    readback || stop "read-back failed"
    if [ "$APPLY" = 1 ]; then
      [ "$BLOCKED" = 0 ] || stop "$BLOCKED node group(s) still block TCP 25"
      mark legacy-outbound-restore groups "$OPENED"
    fi ;;
esac
finish
