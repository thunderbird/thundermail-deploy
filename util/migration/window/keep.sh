#!/usr/bin/env bash
set -euo pipefail
MODE=${1:-}
case "$MODE" in
  save|restore) shift ;;
  *) echo "usage: TARGET_ENV=<env> keep.sh save | restore [--dry-run | --apply]" >&2; exit 64 ;;
esac
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need SECRET_DIR EVIDENCE_DIR
KEEP_DIR=$EVIDENCE_DIR/keep
KEEP_FILES="export.json principals.json settings.json domains-dropped.json domain-fixed.txt duplicates.txt source-branch.env"
GPG=(gpg --batch --yes --quiet)

recipient() {
  [ -n "${KEEP_GPG_RECIPIENT:-}" ] && return 0
  echo "KEEP_GPG_RECIPIENT is empty in $ENV_FILE: set it in the operator file (a gpg key of the operator) and rebuild the env file; without it the files of $SECRET_DIR exist only on tmpfs"
  [ "$APPLY" = 1 ] && return 1
  printf 'NEED  KEEP_GPG_RECIPIENT for the encrypted copies of %s\n' "$SECRET_DIR"
  NEEDS=$((NEEDS + 1))
  return 1
}

save() {
  local f n=0 sum
  umask 077
  printf '  + for each of %s present in %s: gpg --encrypt --recipient %s to %s/<name>.gpg (mode 600), sha256 of the plain file in %s/manifest.tsv\n' "$KEEP_FILES" "$SECRET_DIR" "${KEEP_GPG_RECIPIENT:-<KEEP_GPG_RECIPIENT>}" "$KEEP_DIR" "$KEEP_DIR"
  recipient || { [ "$APPLY" = 0 ]; return; }
  [ "$APPLY" = 1 ] || return 0
  ( umask 077; mkdir -p "$KEEP_DIR" ) || return 1
  for f in $KEEP_FILES; do
    [ -s "$SECRET_DIR/$f" ] || continue
    sum=$(sha256sum "$SECRET_DIR/$f" | cut -d' ' -f1)
    ( umask 077; "${GPG[@]}" --trust-model always --recipient "$KEEP_GPG_RECIPIENT" --encrypt --output "$KEEP_DIR/$f.gpg.part" "$SECRET_DIR/$f" ) || { echo "gpg could not encrypt $f for $KEEP_GPG_RECIPIENT"; rm -f "$KEEP_DIR/$f.gpg.part"; return 1; }
    mv "$KEEP_DIR/$f.gpg.part" "$KEEP_DIR/$f.gpg"
    { grep -v "^$f"$'\t' "$KEEP_DIR/manifest.tsv" 2>/dev/null || true; printf '%s\t%s\n' "$f" "$sum"; } > "$KEEP_DIR/manifest.tsv.part"
    mv "$KEEP_DIR/manifest.tsv.part" "$KEEP_DIR/manifest.tsv"
    printf 'kept %s (sha256 %s) as %s\n' "$f" "${sum:0:12}" "$KEEP_DIR/$f.gpg"
    n=$((n + 1))
  done
  VALUE=$n
  echo "keep: $n file(s) of $SECRET_DIR encrypted in $KEEP_DIR"
}

restore() {
  local f want have n=0 bad=0
  umask 077
  printf '  + for each %s/<name>.gpg: gpg --decrypt into %s/<name> (mode 600) unless the plain file is there with the sha256 of %s/manifest.tsv\n' "$KEEP_DIR" "$SECRET_DIR" "$KEEP_DIR"
  [ -s "$KEEP_DIR/manifest.tsv" ] || { echo "no $KEEP_DIR/manifest.tsv: nothing was kept in this attempt"; return 1; }
  while IFS=$'\t' read -r f want; do
    case "$f" in ''|*/*) continue ;; esac
    have=$( [ -f "$SECRET_DIR/$f" ] && sha256sum "$SECRET_DIR/$f" | cut -d' ' -f1 || true)
    if [ "$have" = "$want" ]; then printf 'present %s (sha256 %s)\n' "$f" "${want:0:12}"; continue; fi
    printf 'restore %s from %s\n' "$f" "$KEEP_DIR/$f.gpg"
    [ "$APPLY" = 1 ] || continue
    ( umask 077; mkdir -p "$SECRET_DIR"; "${GPG[@]}" --decrypt --output "$SECRET_DIR/$f.part" "$KEEP_DIR/$f.gpg" ) || { echo "gpg could not decrypt $f"; rm -f "$SECRET_DIR/$f.part"; bad=1; continue; }
    have=$(sha256sum "$SECRET_DIR/$f.part" | cut -d' ' -f1)
    [ "$have" = "$want" ] || { echo "$f: sha256 ${have:0:12} after the decrypt, manifest ${want:0:12}"; rm -f "$SECRET_DIR/$f.part"; bad=1; continue; }
    mv "$SECRET_DIR/$f.part" "$SECRET_DIR/$f"
    printf 'restored %s (sha256 %s)\n' "$f" "${want:0:12}"
    n=$((n + 1))
  done < "$KEEP_DIR/manifest.tsv"
  VALUE=$n
  [ "$bad" = 0 ]
}

step "keep-$MODE" files "$MODE"
finish
