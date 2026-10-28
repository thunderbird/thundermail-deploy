#!/usr/bin/env bash
set -euo pipefail
DRY=0
for a in "$@"; do [ "$a" = --dry-run ] && DRY=1; done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
EXPORT=${1:-}
[ -s "$EXPORT" ] || die "usage: retarget-export.sh export.json [--dry-run]"
need TARGET_BLOB_BUCKET REDIS_URL
VALUES=$REPO_DIR/overlays/tb-stage-thundermail/stalwart-configs/config/values.yaml
region=$(yq -r '.blobStoreRegion // ""' "$VALUES")
[ -n "$region" ] || die "blobStoreRegion missing in $VALUES"
for o in BlobStore InMemoryStore; do
  [ "$(jq -s --arg o "$o" '[.[] | select(.object == $o)] | length' "$EXPORT")" = 1 ] || die "$EXPORT does not hold exactly one $o update"
done
before=$(jq -r 'select(.object == "BlobStore" or .object == "InMemoryStore") | "\(.object)=\(.value["@type"])"' "$EXPORT" | tr '\n' ' ')
tmp=$(mktemp "$EXPORT.XXXXXX")
trap 'rm -f "$tmp"' EXIT
jq -c --arg bucket "$TARGET_BLOB_BUCKET" --arg region "$region" --arg url "$REDIS_URL" '
  if .object == "BlobStore" then .value = {"@type": "S3", region: {"@type": $region}, bucket: $bucket,
      accessKey: {"@type": "None"}, secretKey: {"@type": "None"}, securityToken: {"@type": "None"}, sessionToken: {"@type": "None"}}
  elif .object == "InMemoryStore" then .value = {"@type": "Redis", url: $url}
  else . end' "$EXPORT" > "$tmp"
[ "$(grep -c . "$tmp")" = "$(grep -c . "$EXPORT")" ] || die "line count changed while rewriting $EXPORT"
host=$(sed -E 's|^[a-z]+://([^@/]*@)?([^:/]+).*|\2|' <<<"$REDIS_URL")
printf 'before: %s\nafter:  BlobStore=S3 bucket=%s region=%s keys=None InMemoryStore=Redis host=%s\n' "$before" "$TARGET_BLOB_BUCKET" "$region" "$host"
if [ "$DRY" = 1 ]; then echo "mode=dry-run $EXPORT unchanged"; exit 0; fi
[ -e "$EXPORT.pre-retarget" ] || cp -p "$EXPORT" "$EXPORT.pre-retarget"
mv "$tmp" "$EXPORT"
echo "mode=apply rewrote $EXPORT (original kept as $EXPORT.pre-retarget)"
