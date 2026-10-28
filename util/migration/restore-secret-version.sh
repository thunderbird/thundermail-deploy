#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ID=${1:-}; TARGET=${2:-}
[ -n "$ID" ] && [ -n "$TARGET" ] || die "usage: restore-secret-version.sh SECRET_ID VERSION_ID [--apply]"
stages=$(aws_stage secretsmanager describe-secret --secret-id "$ID" | jq -c '.VersionIdsToStages')
current=$(jq -r 'to_entries[] | select(.value | index("AWSCURRENT")) | .key' <<<"$stages")
aws_stage secretsmanager list-secret-versions --secret-id "$ID" --include-deprecated --output json | jq -e --arg v "$TARGET" 'any(.Versions[]; .VersionId == $v)' >/dev/null || die "version $TARGET not found on $ID"
printf 'secret=%s current=%s target=%s\n' "$ID" "$current" "$TARGET"
if [ "$current" = "$TARGET" ]; then echo 'already AWSCURRENT: nothing to do'; exit 0; fi
if [ "$APPLY" = 1 ]; then
  aws_stage secretsmanager update-secret-version-stage --secret-id "$ID" --version-stage AWSCURRENT --move-to-version-id "$TARGET" --remove-from-version-id "$current" --query Name --output text >/dev/null
  echo 'mode=apply AWSCURRENT moved'
else
  echo 'mode=dry-run would move AWSCURRENT to the target version'
fi
