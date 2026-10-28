#!/usr/bin/env bash
set -euo pipefail
DRY=0
for a in "$@"; do [ "$a" = --dry-run ] && DRY=1; done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD KARGO_PROJECT_CONFIGS KARGO_STAGE_CONFIGS
KINDS="BlockedIp AllowedIp Domain Security Http MtaQueueQuota Calendar AddressBook WebHook BlobStore InMemoryStore AcmeProvider Directory Tenant DnsServer Role"
mkdir -p "$SECRET_DIR"
settings=$SECRET_DIR/settings-before-$TS.ndjson
prefix=""
[ "$DRY" = 0 ] || prefix=dryrun-
freight=$EVIDENCE_DIR/${prefix}freight-before-$TS.txt
export STALWART_URL STALWART_USER STALWART_PASSWORD
if [ "$DRY" = 1 ]; then
  printf 'dry-run: would run stalwart-cli snapshot %s --output %s\n' "$KINDS" "$settings"
  domains=$(stalwart-cli query Domain --fields id 2>/dev/null) || { echo "domains visible: unreachable (stalwart-cli could not query $STALWART_URL)"; exit 1; }
  printf 'domains visible: %s\n' "$(grep -c . <<<"$domains" || true)"
else
  stalwart-cli snapshot $KINDS --output "$settings"
  printf 'settings objects=%s\n%s\n' "$(grep -c . "$settings")" "$settings"
fi
khub -n "$KARGO_PROJECT_CONFIGS" get stage "$KARGO_STAGE_CONFIGS" -o jsonpath='{.status.freightHistory[0].items.*.name}' > "$freight"
printf 'freight=%s\n%s\n' "$(cat "$freight")" "$freight"
