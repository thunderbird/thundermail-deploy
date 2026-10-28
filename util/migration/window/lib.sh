. "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/../timing.sh"
export TS
GO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --go) [ -n "${2:-}" ] || die "--go needs the operator's words"; GO=$2; shift 2 ;;
    -h|--help) echo "usage: $(basename "$0") [--dry-run | --apply [--go 'operator words']]"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
W=$(basename "$0" .sh)
KEY=$(tr '[:lower:]' '[:upper:]' <<<"${W%%-*}")
ENV_FILE=${CUTOVER_ENV:-$HOME/.local/state/talos/cutover-tb-stage.env}
SECRET_NAMES="STALWART_TARGET_PG_PASSWORD STALWART_PASSWORD OLD_STALWART_PASSWORD STALWART_LEGACY_PG_PASSWORD ACCOUNTS_LEGACY_PG_PASSWORD KC_LEGACY_PG_PASSWORD KC_LEGACY_ADMIN_CLIENT_SECRET CF_API_TOKEN_AGENT PADDLE_API_KEY TEST_MAILBOX_IMAP_PASSWORD TEST_SENDER_APP_PASSWORD"
KS=(kubectl --kubeconfig "${KUBECONFIG_STAGE:-}")
RESTORE=$EVIDENCE_DIR/$W-restore.txt
LOG=$EVIDENCE_DIR/$W-$(mode)-$TS.log
NEEDS=0
PF=""
exec > >(tee -a "$LOG") 2>&1
trap 'stop "command failed at line $LINENO"' ERR
trap '[ -z "$PF" ] || kill "$PF" 2>/dev/null || true' EXIT

sn() { if [ "$APPLY" = 1 ]; then printf '%s' "$1"; else printf 'dryrun-%s' "$1"; fi; }
row() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$(printf '%s' "$5" | tr '\t\n\r' '   ')" >> "$TIMING_TSV"; }
again() { local l; l=$(tail -1 "$TIMING_TSV"); row "$(cut -f1 <<<"$l")" "$(cut -f2 <<<"$l")" "$(cut -f3 <<<"$l")" "$1" "$2"; }
cq() { local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }
curl_user() { printf 'user = "%s:%s"\n' "$(cq "$1")" "$(cq "$2")"; }
doh() { curl -s -m 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=A" | jq -r '[.Answer[]? | select(.type == 1) | .data][0] // empty'; }

shq() {
  local a n v w out=""
  for a in "$@"; do
    w=""
    for n in $SECRET_NAMES; do
      v=${!n:-}
      [ ${#v} -ge 4 ] || continue
      if [ "$a" = "$v" ]; then w="\"\$$n\""; break; fi
      a=${a//"$v"/\$$n}
    done
    if [ -z "$w" ]; then
      case "$a" in
        ''|*[!A-Za-z0-9_./:=@%+,-]*) w="'${a//\'/\'\\\'\'}'" ;;
        *) w=$a ;;
      esac
    fi
    out="$out $w"
  done
  printf '%s' "${out# }"
}
run() { printf '  + %s\n' "$(shq "$@")"; [ "$APPLY" = 1 ] || return 0; "$@"; }
run_to() { local f=$1; shift; printf '  + %s > %s 2>&1\n' "$(shq "$@")" "$f"; [ "$APPLY" = 1 ] || return 0; "$@" > "$f" 2>&1; }
run_err() { local f=$1; shift; printf '  + %s 2> %s\n' "$(shq "$@")" "$f"; [ "$APPLY" = 1 ] || return 0; "$@" 2> "$f"; }
until_ok() { local end=$(( $(date +%s) + $1 )); shift; until "$@"; do [ "$(date +%s)" -lt "$end" ] || return 1; sleep 10; done; }

stop() {
  local now; now=$(date -u +%FT%TZ)
  trap - ERR
  printf 'STOP %s (%s): %s\n' "$KEY" "$(mode)" "$*"
  row "$(sn "stop-$KEY")" "$now" "$now" reason "$*"
  if [ -s "$RESTORE" ]; then
    printf 'pre-staged restore options (%s); run one only after the operator says go:\n' "$RESTORE"
    sed 's/^/  /' "$RESTORE"
  fi
  printf 'evidence: %s\n' "$LOG"
  exit 1
}

step() {
  local name; name=$(sn "$1")
  local metric=$2; shift 2
  VALUE=""
  printf '== %s\n' "$name"
  STEP=$name METRIC=$metric t "$@" || stop "$name failed"
  printf '   %s done%s\n' "$name" "${metric:+ ($metric=$VALUE)}"
}

go_gate() {
  local now
  if [ "$APPLY" = 0 ]; then printf '== operator go: the writes below run only with --apply --go "<operator words>"\n'; return 0; fi
  [ -n "$GO" ] || stop "no operator go: rerun with --apply --go '<the operator's words>'"
  now=$(date -u +%FT%TZ)
  row "go-$KEY" "$now" "$now" quote "$GO"
  printf '== operator go recorded in %s: %s\n' "$TIMING_TSV" "$GO"
}

prereqs() {
  local out
  if out=$(bash "$MIG_DIR/check-prereqs.sh" --for "$KEY" 2>&1); then printf '%s\n' "$(tail -1 <<<"$out")"; return 0; fi
  grep '^NEED' <<<"$out" || printf '%s\n' "$out"
  [ "$APPLY" = 1 ] && stop "check-prereqs.sh --for $KEY reports NEED"
  NEEDS=$((NEEDS + 1))
}

helpers() {
  local f miss=""
  for f in "$@"; do [ -e "$MIG_DIR/$f" ] || command -v "$f" >/dev/null 2>&1 || miss="$miss $f"; done
  [ -n "$miss" ] || return 0
  printf 'NEED  missing:%s\n' "$miss"
  [ "$APPLY" = 1 ] && stop "missing:$miss"
  NEEDS=$((NEEDS + 1))
}

live() {
  local f
  for f in "$@"; do
    [ -e "$f" ] || [ -e "$MIG_DIR/$f" ] || command -v "$f" >/dev/null 2>&1 || { printf '  (live read skipped: %s missing)\n' "$f"; return 1; }
  done
}

pending() {
  local v
  for v in "$@"; do
    [ -n "${!v:-}" ] && continue
    [ "$APPLY" = 1 ] && stop "$v is empty in $ENV_FILE"
    printf "PENDING  %s is filled in-window; shown as \${%s} below\n" "$v" "$v"
    printf -v "$v" "\${%s}" "$v"
  done
}

setenv() {
  local q
  printf '  + append %s=%s to %s\n' "$1" "$2" "$ENV_FILE"
  [ "$APPLY" = 1 ] || return 0
  q=$(printf '%s' "$2" | sed "s/'/'\\\\''/g")
  printf "%s='%s'\n" "$1" "$q" >> "$ENV_FILE"
  export "$1=$2"
}

port_forward() {
  local pod
  if curl -sf -m 5 -o /dev/null "$STALWART_URL/healthz/live"; then echo "port-forward already answers on $STALWART_URL"; return 0; fi
  pod=$("${KS[@]}" -n thundermail get pod -l app=stalwart --field-selector=status.phase=Running -o name | head -1)
  [ -n "$pod" ] || { echo "no Running Stalwart pod"; return 1; }
  "${KS[@]}" -n thundermail port-forward "$pod" "${STALWART_URL##*:}:8080" >/dev/null 2>&1 &
  PF=$!
  until_ok 30 curl -sf -m 5 -o /dev/null "$STALWART_URL/healthz/live" || { echo "port-forward to $pod did not answer"; return 1; }
  echo "port-forward $pod -> $STALWART_URL"
}

finish() {
  if [ "$NEEDS" != 0 ]; then printf '%s %s: %s NEED item(s) above; evidence %s\n' "$KEY" "$(mode)" "$NEEDS" "$LOG"; exit 2; fi
  printf '%s %s: done; evidence %s\n' "$KEY" "$(mode)" "$LOG"
}
