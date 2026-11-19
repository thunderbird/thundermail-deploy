#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
USAGE="usage: mail-carry.sh export | import [EXPORT_DIR] [--dry-run | --apply]"
OP=${POS[0]:-}
case "$OP:${#POS[@]}" in export:1|import:1|import:2) ;; *) echo "$USAGE" >&2; exit 64 ;; esac
[ "$TARGET_ENV" = tb-prod ] || { echo "mail-carry.sh runs for TARGET_ENV=tb-prod only" >&2; exit 64; }
[ -z "$GO" ] || { echo "$USAGE (no --go)" >&2; exit 64; }
PY=${PY:-${VENV_PY:-python3}}
FLAG=--dry-run
[ "$APPLY" = 0 ] || FLAG=--apply
WHY=""
SINCE=""
printf 'n/a: the export only reads tb-prod; the import only adds messages to legacy and skips those it already holds\n' > "$RESTORE"

need_or_stop() {
  printf 'NEED  %s\n' "$1"
  [ "$APPLY" = 0 ] || stop "$1"
  NEEDS=$((NEEDS + 1))
}

since() {
  local j
  [ -n "${TM_SCALE_PR:-}" ] || { WHY="TM_SCALE_PR is empty: load the window shell of 0.2"; return 1; }
  j=$(gh pr view "$TM_SCALE_PR" -R thunderbird/thundermail-deploy --json state,mergedAt 2>/dev/null) || { WHY="cannot read thundermail-deploy PR $TM_SCALE_PR"; return 1; }
  SINCE=$(jq -r 'select(.state == "MERGED") | .mergedAt // empty' <<<"$j" 2>/dev/null || true)
  [ -n "$SINCE" ] || { WHY="thundermail-deploy PR $TM_SCALE_PR (the scale-up of 5.1) is not merged: tb-prod has taken no mail"; return 1; }
}

run_py() {
  local rc=0
  printf '  + %s -I %s/mail_carry.py %s %s\n' "$PY" "$MIG_DIR" "$*" "$FLAG"
  TIMING_TSV=$TIMING_TSV "$PY" -I "$MIG_DIR/mail_carry.py" "$@" "$FLAG" || rc=$?
  case $rc in
    0) ;;
    2) [ "$APPLY" = 0 ] || stop "mail_carry.py $1 reports NEED"; NEEDS=$((NEEDS + 1)) ;;
    *) stop "mail_carry.py $1 exited $rc" ;;
  esac
}

newest_export() {
  local d
  for d in $(ls -1dt "$SECRET_DIR"/mail-export-*/ 2>/dev/null || true); do
    [ -s "$d/summary.json" ] && { printf '%s' "${d%/}"; return 0; }
  done
  return 0
}

carry_export() {
  local ok=1
  if since; then
    printf 'since %s: the merge of thundermail-deploy PR %s\n' "$SINCE" "$TM_SCALE_PR"
  else
    need_or_stop "$WHY"; ok=0
  fi
  if ! port_forward; then need_or_stop "no port-forward of $(id -un) to tb-prod Stalwart on $STALWART_URL"; ok=0; fi
  [ "$ok" = 1 ] || return 0
  run_py export --since "$SINCE" --out "$SECRET_DIR/mail-export-$TS"
}

carry_import() {
  local dir=${POS[1]:-} ok=1 code
  [ -n "$dir" ] || dir=$(newest_export)
  [ -n "$dir" ] || { need_or_stop "no complete export (mail-export-*/summary.json) under $SECRET_DIR: run mail-carry.sh export --apply first"; ok=0; }
  if ! WHY=$(legacy_tunnel); then
    need_or_stop "$WHY"; ok=0
  else
    code=$(curl -sk --noproxy '*' -m 10 -o /dev/null -w '%{http_code}' "${OLD_STALWART_URL%/}/healthz/live" || true)
    [ "$code" = 200 ] || { need_or_stop "legacy Stalwart answers ${code:-nothing} on healthz/live through the tunnel: start it first"; ok=0; }
  fi
  [ "$ok" = 1 ] || return 0
  printf 'export: %s\n' "$dir"
  run_py import --from "$dir"
}

printf '== mail-%s (%s)\n' "$OP" "$(mode)"
case "$OP" in
  export) carry_export ;;
  import) carry_import ;;
esac
finish
