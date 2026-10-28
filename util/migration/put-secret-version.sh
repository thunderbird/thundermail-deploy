#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ID=${1:-}; FILE=${2:-}
[ -n "$ID" ] && [ -f "$FILE" ] || die "usage: put-secret-version.sh SECRET_ID FILE.json [--apply]"
jq -e 'type == "object"' "$FILE" >/dev/null || die "$FILE is not a JSON object"
cur=$(aws_stage secretsmanager get-secret-value --secret-id "$ID" --output json | jq -r '.SecretString' | jq -cS . | sha256sum | cut -c1-16)
new=$(jq -cS . "$FILE" | sha256sum | cut -c1-16)
newkeys=$(jq -r 'keys|join(",")' "$FILE")
printf 'secret=%s current_sha=%s new_sha=%s new_keys=%s\n' "$ID" "$cur" "$new" "$newkeys"
if [ "$cur" = "$new" ]; then echo 'unchanged: nothing to put'; exit 0; fi
if [ "$APPLY" = 1 ]; then
  jq -c . "$FILE" > "$FILE.compact"
  vid=$(aws_stage secretsmanager put-secret-value --secret-id "$ID" --secret-string "file://$FILE.compact" --query VersionId --output text)
  rm -f "$FILE.compact"
  printf 'mode=apply new_version=%s\n' "$vid"
else
  echo 'mode=dry-run would put-secret-value (whole JSON via file://)'
fi
