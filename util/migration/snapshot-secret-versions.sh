#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ $# -gt 0 ] || die "usage: snapshot-secret-versions.sh SECRET_ID..."
out=$EVIDENCE_DIR/secret-versions-before-$TS.txt
: > "$out"
for id in "$@"; do
  v=$(aws_stage secretsmanager describe-secret --secret-id "$id" | jq -r '.VersionIdsToStages | to_entries[] | select(.value | index("AWSCURRENT")) | .key')
  [ -n "$v" ] || die "no AWSCURRENT version for $id"
  printf '%s\t%s\n' "$id" "$v" >> "$out"
done
cat "$out"
printf 'secrets=%s\n%s\n' "$#" "$out"
