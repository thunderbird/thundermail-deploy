#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
versions() { aws_stage secretsmanager list-secret-version-ids --secret-id "$1" --include-deprecated --output json | jq -c '[.Versions[].VersionId]'; }
current_of() { aws_stage secretsmanager describe-secret --secret-id "$1" | jq -r '.VersionIdsToStages | to_entries[] | select(.value | index("AWSCURRENT")) | .key'; }
if [ "${1:-}" = --check ]; then
  shift
  [ $# -gt 0 ] || die "usage: restore-secret-version.sh --check SECRET_ID..."
  for id in "$@"; do
    v=$(versions "$id") || die "list-secret-version-ids failed for $id"
    c=$(current_of "$id")
    jq -e --arg c "$c" '$c != "" and index($c) != null' <<<"$v" >/dev/null || die "$id: AWSCURRENT ${c:-none} not among the listed versions"
    printf 'check ok %s versions=%s current=%s\n' "$id" "$(jq length <<<"$v")" "$c"
  done
  exit 0
fi
ID=${1:-}; TARGET=${2:-}
[ -n "$ID" ] && [ -n "$TARGET" ] || die "usage: restore-secret-version.sh SECRET_ID VERSION_ID [--apply] | --check SECRET_ID..."
current=$(current_of "$ID")
jq -e --arg v "$TARGET" 'index($v) != null' <<<"$(versions "$ID")" >/dev/null || die "version $TARGET not found on $ID"
printf 'secret=%s current=%s target=%s\n' "$ID" "$current" "$TARGET"
if [ "$current" = "$TARGET" ]; then echo 'already AWSCURRENT: nothing to do'; exit 0; fi
if [ "$APPLY" = 1 ]; then
  aws_stage secretsmanager update-secret-version-stage --secret-id "$ID" --version-stage AWSCURRENT --move-to-version-id "$TARGET" --remove-from-version-id "$current" --query Name --output text >/dev/null
  echo 'mode=apply AWSCURRENT moved'
else
  echo 'mode=dry-run would move AWSCURRENT to the target version'
fi
