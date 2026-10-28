TS=${TS:-$(date -u +%Y%m%dT%H%M%SZ)}
TIMING_TSV=${TIMING_TSV:-${EVIDENCE_DIR:-$HOME/.local/state/talos/cutover-tb-stage}/timing-tb-stage.tsv}
mkdir -p "$(dirname "$TIMING_TSV")"
t() {
  local s e rc
  s=$(date -u +%FT%TZ)
  if "$@"; then rc=0; else rc=$?; fi
  e=$(date -u +%FT%TZ)
  printf '%s\t%s\t%s\t%s\t%s\n' "${STEP:-}" "$s" "$e" "${METRIC:-}" "${VALUE:-}" >> "$TIMING_TSV"
  return $rc
}
pod() { kubectl --kubeconfig "$KUBECONFIG_STAGE" -n thundermail exec -i runner -c postgres -- "$@"; }
waitfor() { timeout "$1" bash -c "until $2; do sleep 10; done"; }
