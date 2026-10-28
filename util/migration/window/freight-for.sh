#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KARGO_PROJECT_CONFIGS KUBECONFIG_HUB
[ "${#POS[@]}" = 1 ] || die "usage: freight-for.sh SHA (prints the Freight name as the last line)"
freight_for "${POS[0]}" || stop "no Freight to promote for ${POS[0]}"
printf '%s\n' "$FREIGHT_FOUND"
