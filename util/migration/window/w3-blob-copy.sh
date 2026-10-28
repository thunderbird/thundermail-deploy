#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need LEGACY_BLOB_BUCKET TARGET_BLOB_BUCKET SECRET_DIR RCLONE_CONFIG_LEG_TYPE RCLONE_CONFIG_LEG_PROFILE RCLONE_CONFIG_TGT_TYPE RCLONE_CONFIG_TGT_PROFILE
mkdir -p "$SECRET_DIR"
LEG=leg:$LEGACY_BLOB_BUCKET
TGT=tgt:$TARGET_BLOB_BUCKET
TARGET_BEFORE=$EVIDENCE_DIR/blobs-target-before-$TS.txt
TARGET_KEYS=$EVIDENCE_DIR/blobs-target-before-keys-$TS-$$.txt
BASELINE=0
BASELINE_BYTES=0
LEGACY_SIZE=$EVIDENCE_DIR/blobs-legacy-$TS.txt
COPY_LOG=$SECRET_DIR/rclone-copy-$(mode)-$TS.log
COPY=(rclone copy "$LEG" "$TGT" --transfers 64 --checkers 64 --s3-no-check-bucket --fast-list --retries 5 --low-level-retries 20 --stats 60s --stats-one-line --stats-log-level NOTICE --log-level NOTICE --log-file "$COPY_LOG")

first_seen() { awk -F'\t' -v n="$1" '$1 == "cut-ecs" {z = 0} $1 == "snap-blobs" && $4 == "objects" && $5 == n {z = 1} END {exit !z}' "$TIMING_TSV" 2>/dev/null; }
listed() { rclone lsd "$1:" | awk -v b="$2" '$NF == b {f = 1} END {exit !f}'; }

snap_blobs() {
  local foreign=0
  rclone size "$TGT" --json > "$TARGET_BEFORE" || return 1
  VALUE=$(jq -r .count "$TARGET_BEFORE")
  echo "target $TGT objects=$VALUE bytes=$(jq -r .bytes "$TARGET_BEFORE") ($TARGET_BEFORE)"
  if [ "$VALUE" != 0 ]; then
    local listing legacy
    listing=$(rclone lsf "$TGT" -R --files-only --format ps --separator $'\t') || return 1
    legacy=$(rclone lsf "$LEG" -R --files-only | sort) || return 1
    cut -f1 <<<"$listing" > "$TARGET_KEYS"
    foreign=$(comm -12 <(sort "$TARGET_KEYS") <(printf '%s\n' "$legacy") | grep -c . || true)
    read -r BASELINE BASELINE_BYTES < <(awk -F'\t' 'NR == FNR {l[$0] = 1; next} !($1 in l) {n++; b += $2} END {print n + 0, b + 0}' <(printf '%s\n' "$legacy") <(printf '%s\n' "$listing"))
    echo "target keys before the copy: $(grep -c . "$TARGET_KEYS") ($TARGET_KEYS); also in $LEG: $foreign; baseline (not in $LEG): $BASELINE object(s), $BASELINE_BYTES bytes"
  fi
  if restore_staged; then
    echo "restore already staged for this cut: $RESTORE"
  elif [ "$VALUE" = 0 ]; then
    printf 'rclone delete %s --dry-run\nrclone delete %s\n' "$TGT" "$TGT" > "$RESTORE" || return 1
    mark_restore
  elif [ "$foreign" = 0 ]; then
    printf 'rclone delete %s --exclude-from %s --dry-run\nrclone delete %s --exclude-from %s\n' "$TGT" "$TARGET_KEYS" "$TGT" "$TARGET_KEYS" > "$RESTORE" || return 1
    mark_restore
    echo "baseline: $BASELINE object(s) Stalwart wrote before the copy (none is in $LEG, e.g. the web-admin bundle); the staged restore keeps them"
    return 0
  else
    printf 'none staged: %s held %s objects before the copy, so rclone delete is not safe\n' "$TGT" "$VALUE" > "$RESTORE" || return 1
    mark_restore
  fi
  if [ "$APPLY" = 0 ] || [ "$VALUE" = 0 ]; then return 0; fi
  first_seen "$BASELINE" || { echo "target bucket is not empty and no snap-blobs row for this cut saw only the $BASELINE baseline object(s)"; return 1; }
  echo "resuming: an earlier snap-blobs row for this cut saw only the $BASELINE baseline object(s)"
}

legacy_size() {
  listed leg "$LEGACY_BLOB_BUCKET" || { echo "$LEGACY_BLOB_BUCKET not listed by rclone lsd leg:"; return 1; }
  listed tgt "$TARGET_BLOB_BUCKET" || { echo "$TARGET_BLOB_BUCKET not listed by rclone lsd tgt:"; return 1; }
  rclone size "$LEG" --json > "$LEGACY_SIZE" || return 1
  VALUE=$(jq -r .count "$LEGACY_SIZE")
  echo "legacy $LEG objects=$VALUE bytes=$(jq -r .bytes "$LEGACY_SIZE") ($LEGACY_SIZE)"
}

copy() {
  local want; want=$(jq -r .count "$LEGACY_SIZE")
  if [ "$APPLY" = 1 ]; then
    run "${COPY[@]}" || { echo "rclone copy failed; log $COPY_LOG"; return 1; }
    grep -aE 'NOTICE: +[0-9.]+ [KMGT]?i?B / ' "$COPY_LOG" | tail -1 | sed 's/^.*NOTICE: */  last stats: /'
    rclone size "$TGT" --json > "$EVIDENCE_DIR/blobs-target-after-$TS.txt" || return 1
    VALUE=$(jq -r .count "$EVIDENCE_DIR/blobs-target-after-$TS.txt")
    BYTES=$(jq -r .bytes "$EVIDENCE_DIR/blobs-target-after-$TS.txt")
  else
    printf '  + %s\n' "$(shq "${COPY[@]}")"
    rclone copy --dry-run "$LEG" "$TGT" --checkers 64 --s3-no-check-bucket --fast-list --log-level NOTICE --log-file "$COPY_LOG" || return 1
    VALUE=$(grep -ac 'Skipped copy as --dry-run is set' "$COPY_LOG" || true)
    BYTES=$(jq -r .bytes "$LEGACY_SIZE")
  fi
  echo "objects=$VALUE (legacy $want) bytes=$BYTES (legacy $(jq -r .bytes "$LEGACY_SIZE"))"
  if [ "$APPLY" = 0 ] && [ "$(jq -r .count "$TARGET_BEFORE")" != "$BASELINE" ]; then echo "target not empty: planned copies are not compared with the legacy count"; return 0; fi
  if [ "$APPLY" = 1 ]; then
    [ "$VALUE" = $((want + BASELINE)) ] || { echo "object count differs from the post-cut legacy rclone size plus the $BASELINE baseline object(s)"; return 1; }
    [ "$BYTES" = $(( $(jq -r .bytes "$LEGACY_SIZE") + BASELINE_BYTES )) ] || { echo "bytes differ from the post-cut legacy rclone size plus the baseline"; return 1; }
  else
    [ "$VALUE" = "$want" ] || { echo "planned copies differ from the post-cut legacy rclone size"; return 1; }
  fi
}

check_blobs() {
  local out rc=0 diffs
  out=$(rclone check --one-way "$LEG" "$TGT" 2>&1) || rc=$?
  printf '%s\n' "$out" > "$SECRET_DIR/rclone-check-$(mode)-$TS.log"
  grep -aE ': [0-9]+ (differences found|matching files|files missing|errors while checking|hashes could not)' <<<"$out" | sed -E 's/^.*(NOTICE|ERROR) *: */  /' || true
  diffs=$(sed -n 's/.* \([0-9][0-9]*\) differences found.*/\1/p' <<<"$out" | tail -1)
  echo "rclone check exit=$rc differences=${diffs:-unreported}"
  if [ "$APPLY" = 0 ]; then VALUE=${diffs:-errors-exit-$rc}; return 0; fi
  if [ "$rc" = 0 ]; then VALUE=0; else VALUE=errors-exit-$rc; return 1; fi
}

prereqs
step snap-blobs objects snap_blobs
step legacy-size objects legacy_size
step blob-copy objects copy
again "$(sn blob-copy)" bytes "$BYTES" || stop "could not write the blob-copy bytes row"
step blob-check differences check_blobs
finish
