#!/bin/bash
# Run the checks of one group (default: all) in order. Exit code is the number of checks that reported FAIL.
# 01 must succeed: the later checks compare against the snapshot it writes, so a stale one would give false PASSes.
set -u
cd "$(dirname "$0")" || exit 99
usage() { echo "usage: run_all.sh [--group source|data|config]"; exit 99; }
GROUP=all
case "${1:-}" in
  --group) GROUP=${2:-} ;;
  "") ;;
  *) usage ;;
esac
case "$GROUP" in
  source) checks="" ;;
  data) checks="02_directory.py 03_messages.py 04_collections.py 05_folders.py 06_buckets.py" ;;
  config) checks="07_target_config.py" ;;
  all) checks="02_directory.py 03_messages.py 04_collections.py 05_folders.py 06_buckets.py 07_target_config.py" ;;
  *) usage ;;
esac
OUT=${VERIFY_OUT:-out}
mkdir -p "$OUT"
rm -f "$OUT"/check-*.txt
if [ "$GROUP" != config ]; then
  rm -f "$OUT/source.json" "$OUT/target-message-blobs.json"
  python3 01_source_snapshot.py > "$OUT/check-01_source_snapshot.txt" 2>&1
  rc=$?
  cat "$OUT/check-01_source_snapshot.txt"
  [ "$rc" = 0 ] || { echo "== 01 failed: stopping, later checks need a fresh source snapshot"; exit 99; }
fi
fails=0
for s in $checks; do
  python3 "$s" > "$OUT/check-${s%.py}.txt" 2>&1 || fails=$((fails + 1))
  cat "$OUT/check-${s%.py}.txt"
done
warns=$(cat "$OUT"/check-*.txt | grep '^  WARN  ' || true)
unexpected=$(printf '%s\n' "$warns" | grep -v '^$' | grep -vFf expected-warn.txt || true)
printf '%s\n' "$unexpected" | grep -v '^$' | sed 's/^  WARN  /  UNEXPECTED WARN  /'
echo "== done ($GROUP): $fails check(s) with FAIL; $(printf '%s\n' "$warns" | grep -c .) WARN, $(printf '%s\n' "$unexpected" | grep -c .) outside expected-warn.txt"
exit $fails
