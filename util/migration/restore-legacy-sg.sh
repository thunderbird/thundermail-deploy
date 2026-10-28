#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SNAP=${1:-}
[ -f "$SNAP" ] || die "usage: restore-legacy-sg.sh legacy-sg-before-<ts>.json [--apply]"
jq -e '(.group.GroupId | type == "string") and (.rules | type == "array" and length > 0)' "$SNAP" >/dev/null || die "$SNAP is not a security group snapshot"
SG=$(jq -r '.group.GroupId' "$SNAP")
key='"\(.IpProtocol)|\(.FromPort // -1)|\(.ToPort // -1)|\(.CidrIpv4 // .CidrIpv6 // .ReferencedGroupInfo.GroupId // .PrefixListId // "")"'
current=$(aws_legacy ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG" --query 'SecurityGroupRules[?IsEgress==`false`]' | jq -r ".[] | $key")
fail=0; n=0
while IFS= read -r rule; do
  [ -n "$rule" ] || continue
  k=$(jq -r "$key" <<<"$rule")
  grep -qxF "$k" <<<"$current" && continue
  n=$((n + 1))
  perm=$(jq -c '{IpProtocol} + (if .FromPort != null then {FromPort, ToPort} else {} end)
    + (if .CidrIpv4 then {IpRanges: [{CidrIp: .CidrIpv4, Description: (.Description // "")}]}
       elif .CidrIpv6 then {Ipv6Ranges: [{CidrIpv6: .CidrIpv6, Description: (.Description // "")}]}
       elif .PrefixListId then {PrefixListIds: [{PrefixListId: .PrefixListId, Description: (.Description // "")}]}
       else {UserIdGroupPairs: [{GroupId: .ReferencedGroupInfo.GroupId, Description: (.Description // "")}]} end)' <<<"$rule")
  printf 'AUTHORIZE %s %s\n' "$SG" "$k"
  [ "$APPLY" = 1 ] || continue
  aws_legacy ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions "[$perm]" --query 'Return' --output text >/dev/null || fail=$((fail + 1))
done < <(jq -c '.rules[] | select(.IsEgress == false)' "$SNAP")
printf 'mode=%s sg=%s saved=%s present=%s to_authorize=%s failed=%s\n' "$(mode)" "$SG" "$(jq '[.rules[]|select(.IsEgress==false)]|length' "$SNAP")" "$(grep -c . <<<"$current" || true)" "$n" "$fail"
[ "$fail" = 0 ]
