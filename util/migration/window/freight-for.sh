#!/usr/bin/env bash
set -euo pipefail
SHA=${1:-}
[ -z "$SHA" ] || shift
. "$(dirname "${BASH_SOURCE[0]}")/../target.sh"
[[ $SHA =~ ^[0-9a-f]{7,40}$ ]] || { echo "usage: TARGET_ENV=<env> freight-for.sh <commit on main>" >&2; exit 64; }
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KARGO_PROJECT_CONFIGS KUBECONFIG_HUB
match() { freight_for "$SHA" && VALUE=$FREIGHT_FOUND; }
step freight-for freight match
finish
