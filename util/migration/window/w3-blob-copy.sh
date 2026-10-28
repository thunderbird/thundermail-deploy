#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need LEGACY_BLOB_BUCKET TARGET_BLOB_BUCKET SECRET_DIR RCLONE_CONFIG_LEG_TYPE RCLONE_CONFIG_LEG_PROFILE RCLONE_CONFIG_TGT_TYPE RCLONE_CONFIG_TGT_PROFILE
mkdir -p "$SECRET_DIR"
LEG=leg:$LEGACY_BLOB_BUCKET
TGT=tgt:$TARGET_BLOB_BUCKET
TARGET_BEFORE=$EVIDENCE_DIR/blobs-target-before-$TS.txt
LEGACY_SIZE=$EVIDENCE_DIR/blobs-legacy-$TS.txt
COPY_LOG=$SECRET_DIR/rclone-copy-$(mode)-$TS.log
COPY=(rclone copy "$LEG" "$TGT" --transfers 64 --checkers 64 --s3-no-check-bucket --fast-list --retries 5 --low-level-retries 20 --stats 60s --stats-one-line --stats-log-level NOTICE --log-level NOTICE --log-file "$COPY_LOG")

listed() { rclone lsd "$1:" | awk -v b="$2" '$NF == b {f = 1} END {exit !f}'; }

snap_blobs() {
  rclone size "$TGT" --json > "$TARGET_BEFORE" || return 1
  VALUE=$(jq -r .count "$TARGET_BEFORE")
  echo "target $TGT objects=$VALUE bytes=$(jq -r .bytes "$TARGET_BEFORE") ($TARGET_BEFORE)"
  if [ "$VALUE" = 0 ]; then
    printf 'rclone delete %s --dry-run\nrclone delete %s\n' "$TGT" "$TGT" > "$RESTORE"
  else
    printf 'none staged: %s held %s objects before the copy, so rclone delete is not safe\n' "$TGT" "$VALUE" > "$RESTORE"
  fi
  [ "$APPLY" = 0 ] || [ "$VALUE" = 0 ] || { echo "target bucket is not empty before the copy"; return 1; }
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
    grep -a 'Transferred:' "$COPY_LOG" | tail -1 | sed 's/^.*NOTICE: */  last stats: /'
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
  [ "$VALUE" = "$want" ] || { echo "object count differs from the post-cut legacy rclone size"; return 1; }
  [ "$BYTES" = "$(jq -r .bytes "$LEGACY_SIZE")" ] || { echo "bytes differ from the post-cut legacy rclone size"; return 1; }
}

check_blobs() {
  local out rc=0
  out=$(rclone check "$LEG" "$TGT" 2>&1) || rc=$?
  printf '%s\n' "$out" > "$SECRET_DIR/rclone-check-$(mode)-$TS.log"
  grep -aE 'differences found|matching files|files missing|missing on|hashes could not' <<<"$out" | grep -v 'ERROR' | sed 's/^.*NOTICE: */  /' || true
  VALUE=$(sed -n 's/.* \([0-9][0-9]*\) differences found.*/\1/p' <<<"$out" | tail -1)
  [ -n "$VALUE" ] || { echo "rclone check printed no difference count (exit $rc)"; return 1; }
  [ "$APPLY" = 0 ] || { [ "$rc" = 0 ] && [ "$VALUE" = 0 ]; }
}

prereqs
step snap-blobs objects snap_blobs
step legacy-size objects legacy_size
step blob-copy objects copy
again bytes "$BYTES"
step blob-check differences check_blobs
finish
