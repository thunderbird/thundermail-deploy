#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SNAP=${1:-}
[ -f "$SNAP" ] || die "usage: restore-redirect.sh redirect-before-<zone>-<ts>.json [--apply]"
ZONE=$(basename "$SNAP" | sed -n 's/^redirect-before-\([0-9a-f]\{32\}\)-.*/\1/p')
[ -n "$ZONE" ] || die "zone id not in the file name"
jq -e '.rules | type == "array"' "$SNAP" >/dev/null || die "$SNAP is not a redirect snapshot"
need CF_API_TOKEN_AGENT
URL=$CF_API/zones/$ZONE/rulesets/phases/http_request_dynamic_redirect/entrypoint
printf 'current: %s\n' "$(cf_api "$URL" | jq -c '[.result.rules[]? | .ref // .id]')"
printf 'restore: %s\n' "$(jq -c '[.rules[] | .ref // .description]' "$SNAP")"
[ "$APPLY" = 1 ] || { echo "mode=dry-run"; exit 0; }
r=$(cf_api -X PUT "$URL" --data @"$SNAP")
jq -e '.success' <<<"$r" >/dev/null || die "Cloudflare API: $(jq -c '.errors' <<<"$r")"
echo "mode=apply restored $(jq '.rules | length' "$SNAP") rule(s)"
