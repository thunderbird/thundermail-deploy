. "$(dirname "${BASH_SOURCE[0]}")/target.sh" || return 64
TS=${TS:-$(date -u +%Y%m%dT%H%M%SZ)}
TIMING_TSV=${TIMING_TSV:-${EVIDENCE_DIR:-$(evidence_default)}/timing-$TARGET_ENV.tsv}
mkdir -p "$(dirname "$TIMING_TSV")"
t() {
  local s e rc
  s=$(date -u +%FT%TZ)
  if "$@"; then rc=0; else rc=$?; fi
  e=$(date -u +%FT%TZ)
  [ "$rc" = 0 ] || [ "$TARGET_ENV" != tb-prod ] || return $rc
  printf '%s\t%s\t%s\t%s\t%s\n' "${STEP:-}" "$s" "$e" "${METRIC:-}" "${VALUE:-}" >> "$TIMING_TSV"
  return $rc
}
pod() { kubectl --kubeconfig "${KUBECONFIG_TARGET:-}" -n thundermail exec -i runner -c postgres -- "$@"; }
waitfor() { timeout "$1" bash -c "until $2; do sleep 10; done"; }
