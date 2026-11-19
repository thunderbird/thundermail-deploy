#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
case "$#:${1:-}" in 2:copy|2:check) ;; *) die "usage: blob-delta.sh copy|check OUT_PREFIX [--dry-run | --apply]" ;; esac
need LEGACY_BLOB_BUCKET TARGET_BLOB_BUCKET RCLONE_CONFIG_LEG_TYPE RCLONE_CONFIG_TGT_TYPE
P=$2
LEG=leg:$LEGACY_BLOB_BUCKET
TGT=tgt:$TARGET_BLOB_BUCKET
if [ "$1" = copy ]; then
  CMD=(rclone copy "$LEG" "$TGT" --checksum --transfers 32 --checkers 16 --s3-no-check-bucket --retries 5 --low-level-retries 20 --use-json-log --log-level INFO --stats 60s --stats-log-level NOTICE --log-file "$P.log")
else
  CMD=(rclone check "$LEG" "$TGT" --one-way --checkers 16 --combined "$P-combined.txt" --missing-on-dst "$P-missing.txt" --differ "$P-differ.txt" --error "$P-error.txt" --log-level NOTICE --log-file "$P.log")
fi
printf '  +'; printf ' %q' "${CMD[@]}"; printf '\n'
[ "$APPLY" = 1 ] || exit 0
rc=0
"${CMD[@]}" || rc=$?
if [ "$1" = copy ]; then
  stats=$(jq -cR 'fromjson? | .stats // empty' "$P.log" | tail -1)
  [ -n "$stats" ] || { echo "rclone copy exit=$rc, no stats line in $P.log"; exit 1; }
  echo "rclone copy exit=$rc copied=$(jq -r .transfers <<<"$stats") bytes=$(jq -r .bytes <<<"$stats") errors=$(jq -r .errors <<<"$stats") checks=$(jq -r .checks <<<"$stats") (log $P.log)"
  exit "$rc"
fi
n() { if [ -f "$1" ]; then grep -c . "$1" || true; else echo missing; fi; }
m=$(n "$P-missing.txt") d=$(n "$P-differ.txt") e=$(n "$P-error.txt")
h=$(sed -n 's/.*: \([0-9]*\) hashes could not be checked.*/\1/p' "$P.log" 2>/dev/null | tail -1 || true)
diffs=unreported
case "$m$d" in *[!0-9]*) ;; *) diffs=$((m + d)) ;; esac
echo "rclone check exit=$rc differences=$diffs missing=$m differ=$d errors=$e matching=$(grep -c '^= ' "$P-combined.txt" 2>/dev/null || true) unhashed=${h:-0} (files $P-*.txt)"
[ "$rc" = 0 ] && [ "$diffs" = 0 ] && [ "$e" = 0 ]
