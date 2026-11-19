#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../target.sh"
case "${1:-}" in
  stop-legacy|merge-recovery|verify-compare) GATE=$1 ;;
  *) echo "usage: TARGET_ENV=<env> gate.sh stop-legacy | merge-recovery | verify-compare" >&2; exit 64 ;;
esac
load_cutover_env
TSV=${TIMING_TSV:-${EVIDENCE_DIR:-$(evidence_default)}/timing-$TARGET_ENV.tsv}
[ -r "$TSV" ] || { printf 'WAIT  %s: no timing file %s\n' "$GATE" "$TSV"; exit 2; }

rows() { awk -F'\t' 'BEGIN {OFS = "\t"} $1 == "cut-ecs" {n = 0; delete r} {r[++n] = NR "\t" $0} END {for (i = 1; i <= n; i++) print r[i]}' "$TSV"; }
last() { rows | awk -F'\t' -v s="$1" '$2 == s {l = $0} END {print l}'; }
last_of() { rows | awk -F'\t' -v s="$1" '$2 ~ ("^(" s ")$") {l = $0} END {print l}'; }
line() { cut -f1 <<<"$1"; }
at() { cut -f4 <<<"$1"; }
value() { cut -f6 <<<"$1"; }
go() { printf 'GO    %s: %s\n' "$GATE" "$1"; exit 0; }
wait_() { printf 'WAIT  %s: %s\n' "$GATE" "$1"; exit 2; }

[ -n "$(last cut-ecs)" ] || wait_ "no cut-ecs row in $TSV: W2 has not cut legacy in this attempt"

case "$GATE" in
  stop-legacy)
    read=$(last w4-read-done) q=$(last queue-drain) s=$(last w4-export-start)
    [ -n "$read" ] || wait_ "no w4-read-done row after the last cut-ecs row: the read phase has not ended"
    [ -n "$q" ] && [ "$(line "$q")" -lt "$(line "$read")" ] || wait_ "no queue-drain row before the w4-read-done row"
    [ "$(value "$q")" = 0 ] || wait_ "the last queue-drain row has value $(value "$q"), not 0"
    [ -z "$s" ] || [ "$(line "$s")" -lt "$(line "$read")" ] || wait_ "a read phase started at $(at "$s") after the last w4-read-done row and has not ended: finish --phase read"
    go "queue-drain 0 at $(at "$q"), w4-read-done at $(at "$read"); legacy Stalwart may be stopped" ;;
  merge-recovery)
    r=$(last reset) p=$(last pre-delete) l=$(last_of 'reset|pre-delete|recovery-wait|stop-W4') a=$(last apply-start) e=$(last export-applied)
    [ -n "$r" ] || wait_ "no reset row after the last cut-ecs row"
    [ -n "$p" ] && [ "$(line "$p")" -gt "$(line "$r")" ] || wait_ "no pre-delete row after the reset at $(at "$r")"
    [ -z "$a" ] || [ "$(line "$a")" -lt "$(line "$p")" ] || { [ -n "$e" ] && [ "$(line "$e")" -gt "$(line "$a")" ]; } || wait_ "an apply started at $(at "$a") after this pre-delete and did not finish: the retry is a revert of the recovery-mode PR and a new reset (--phase reset --apply --go)"
    case "$(cut -f2 <<<"$l")" in
      recovery-wait) go "recovery-wait at $(at "$l") after the pre-delete at $(at "$p"); merge the recovery-mode PR" ;;
      stop-W4) wait_ "stop-W4 at $(at "$l") is the last row: resume with --phase boot --apply, then read this gate again" ;;
      *) wait_ "the last of reset, pre-delete, recovery-wait, stop-W4 is $(cut -f2 <<<"$l"): W4 is not waiting for the pod yet" ;;
    esac ;;
  verify-compare)
    f=$(rows | awk -F'\t' '$2 == "verify-compare" && $5 == "fails" {l = $0} END {print l}')
    u=$(rows | awk -F'\t' '$2 == "verify-compare" && $5 == "unexpected_warns" {l = $0} END {print l}')
    s=$(last stop-W6)
    [ -n "$f" ] && [ -n "$u" ] || wait_ "no verify-compare rows after the last cut-ecs row: run w6-verify.sh --apply --compare-only"
    [ "$(line "$u")" -gt "$(line "$f")" ] || wait_ "the last verify-compare fails row has no unexpected_warns row after it"
    [ -z "$s" ] || [ "$(line "$s")" -lt "$(line "$f")" ] || wait_ "stop-W6 at $(at "$s") after the last verify-compare row"
    [ "$(value "$f")" = 0 ] && [ "$(value "$u")" = 0 ] || wait_ "verify-compare fails $(value "$f"), unexpected_warns $(value "$u")"
    go "verify-compare fails 0, unexpected_warns 0 at $(at "$u")" ;;
esac
