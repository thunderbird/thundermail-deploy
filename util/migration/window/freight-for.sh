#!/usr/bin/env bash
set -euo pipefail
SHA=${1:-}
[ -z "$SHA" ] || shift
. "$(dirname "${BASH_SOURCE[0]}")/../target.sh"
[[ $SHA =~ ^[0-9a-f]{7,40}$ ]] || { echo "usage: TARGET_ENV=<env> freight-for.sh <commit on main>" >&2; exit 64; }
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KARGO_PROJECT_CONFIGS KUBECONFIG_HUB
FREIGHT_LINE=""
match() {
  local out rc=0
  out=$(freight_for "$SHA") || rc=$?
  printf '%s\n' "$out"
  [ "$rc" = 0 ] || return 1
  FREIGHT_LINE=$(tail -1 <<<"$out")
  VALUE=${FREIGHT_LINE#Freight }; VALUE=${VALUE%% *}
}
last_lines() { [ -z "$FREIGHT_LINE" ] || printf '%s\n%s\n' "$FREIGHT_LINE" "${FREIGHT_LINE#Freight }" | sed '2s/ .*//'; }
step freight-for freight match
finish
last_lines
