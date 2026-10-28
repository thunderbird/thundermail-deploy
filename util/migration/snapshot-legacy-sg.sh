#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SG=${1:-${LEGACY_PUB_SG:-}}
[ -n "$SG" ] || die "usage: snapshot-legacy-sg.sh [SG_ID] (default: LEGACY_PUB_SG)"
out=$EVIDENCE_DIR/legacy-sg-before-$TS.json
jq -n --argjson g "$(aws_legacy ec2 describe-security-groups --group-ids "$SG" --query 'SecurityGroups[0]')" \
      --argjson r "$(aws_legacy ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG" --query 'SecurityGroupRules')" \
      '{group: $g, rules: $r}' > "$out"
printf 'sg=%s ingress_rules=%s egress_rules=%s\n%s\n' "$SG" "$(jq '[.rules[]|select(.IsEgress==false)]|length' "$out")" "$(jq '[.rules[]|select(.IsEgress==true)]|length' "$out")" "$out"
