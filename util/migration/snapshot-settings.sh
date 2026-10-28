#!/usr/bin/env bash
set -euo pipefail
DRY=0
for a in "$@"; do [ "$a" = --dry-run ] && DRY=1; done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD KARGO_PROJECT_CONFIGS KARGO_STAGE_CONFIGS
KINDS="BlockedIp AllowedIp Domain Security Http MtaQueueQuota Calendar AddressBook WebHook"
mkdir -p "$SECRET_DIR"
settings=$SECRET_DIR/settings-before-$TS.ndjson
freight=$EVIDENCE_DIR/freight-before-$TS.txt
export STALWART_URL STALWART_USER STALWART_PASSWORD
if [ "$DRY" = 1 ]; then
  printf 'dry-run: would run stalwart-cli snapshot %s --output %s\n' "$KINDS" "$settings"
  printf 'domains visible: %s\n' "$(stalwart-cli query Domain --fields id 2>/dev/null | grep -c . || echo 0)"
else
  stalwart-cli snapshot $KINDS --output "$settings"
  printf 'settings objects=%s\n%s\n' "$(grep -c . "$settings")" "$settings"
fi
khub -n "$KARGO_PROJECT_CONFIGS" get stage "$KARGO_STAGE_CONFIGS" -o jsonpath='{.status.freightHistory[0].items.*.name}' > "$freight"
printf 'freight=%s\n%s\n' "$(cat "$freight")" "$freight"
