# shellcheck shell=bash
ENV_FILE=${CUTOVER_ENV:-}
if [ -z "$ENV_FILE" ]; then
  for ENV_FILE in /dev/shm/talos/cutover-tb-stage.env "$HOME/.local/state/talos/cutover-tb-stage.env"; do [ -f "$ENV_FILE" ] && break; done
fi
if [ -f "$ENV_FILE" ]; then
  if [ -L "$ENV_FILE" ] || [ -L "$(dirname "$ENV_FILE")" ] || [ ! -O "$ENV_FILE" ] || [ ! -O "$(dirname "$ENV_FILE")" ] || [ -n "$(find "$ENV_FILE" "$(dirname "$ENV_FILE")" -maxdepth 0 -perm /022)" ]; then
    echo "refusing to load $ENV_FILE: a symlink, not owned by $(id -un), or writable by group or others" >&2
    exit 1
  fi
  set -a; eval "$(< "$ENV_FILE")"; set +a
fi
. "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/../timing.sh"
export TS
GO=""
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --go) [ -n "${2:-}" ] || die "--go needs the operator's words"; GO=$2; shift 2 ;;
    -h|--help) echo "usage: $(basename "$0") [--dry-run | --apply [--go 'operator words']]"; exit 0 ;;
    -*) die "unknown argument: $1" ;;
    *) [ "${ALLOW_POS:-0}" = 1 ] || die "unknown argument: $1"; POS+=("$1"); shift ;;
  esac
done
W=$(basename "$0" .sh)
KEY=$(tr '[:lower:]' '[:upper:]' <<<"${W%%-*}")
SECRET_NAMES="STALWART_TARGET_PG_PASSWORD STALWART_PASSWORD OLD_STALWART_PASSWORD STALWART_LEGACY_PG_PASSWORD ACCOUNTS_LEGACY_PG_PASSWORD KC_LEGACY_PG_PASSWORD KC_LEGACY_ADMIN_CLIENT_SECRET CF_API_TOKEN_AGENT PADDLE_API_KEY TEST_MAILBOX_IMAP_PASSWORD TEST_SENDER_APP_PASSWORD"
KS=(kubectl --kubeconfig "${KUBECONFIG_STAGE:-}")
KH=(kubectl --kubeconfig "${KUBECONFIG_HUB:-}")
TMD_URL=https://github.com/thunderbird/thundermail-deploy.git
PREVIEW_BRANCH=env/tb-stage-thundermail-stalwart-configs-preview
CFG_PATHS="overlays/tb-stage-thundermail/stalwart-configs bases/stalwart-configs"
SUF=""
[ "$APPLY" = 1 ] || SUF=.dry-run
RESTORE=$EVIDENCE_DIR/$W-restore$SUF.txt
LOG=$EVIDENCE_DIR/$W-$(mode)-$TS.log
NEEDS=0
PF=""
PF_OK=0
exec > >(exec setsid tee -a "$LOG") 2>&1
trap 'stop "command failed at line $LINENO"' ERR
trap 'stop "interrupted by signal"' INT TERM HUP
trap '[ -z "$PF" ] || kill "$PF" 2>/dev/null || true' EXIT

sn() { if [ "$APPLY" = 1 ]; then printf '%s' "$1"; else printf 'dryrun-%s' "$1"; fi; }
row() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$(printf '%s' "$5" | tr '\t\n\r' '   ')" >> "$TIMING_TSV"; }
mark() { local now; now=$(date -u +%FT%TZ); row "$1" "$now" "$now" "$2" "$3"; }
again() {
  local l
  l=$(awk -F'\t' -v s="$1" '$1 == s {l = $0} END {print l}' "$TIMING_TSV")
  [ -n "$l" ] || return 1
  row "$1" "$(cut -f2 <<<"$l")" "$(cut -f3 <<<"$l")" "$2" "$3"
}
after_cut() { awk -F'\t' -v s="$1" '$1 == "cut-ecs" {l = ""} $1 == s {l = $0} END {if (l != "") print l}' "$TIMING_TSV" 2>/dev/null; }
restore_staged() { [ "$APPLY" = 1 ] && [ -s "$RESTORE" ] && [ -n "$(after_cut "restore-$KEY")" ]; }
mark_restore() { [ "$APPLY" = 0 ] || mark "restore-$KEY" file "$RESTORE"; }
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
  trap - ERR INT TERM HUP
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
  if [ "$APPLY" = 0 ]; then printf '== operator go: the writes below run only with --apply --go "<operator words>"\n'; return 0; fi
  [ -n "$GO" ] || stop "no operator go: rerun with --apply --go '<the operator's words>'"
  mark "go-$KEY" quote "$GO"
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

live_stalwart() { [ "$PF_OK" = 1 ] || { printf '  (live read skipped: no port-forward of %s)\n' "$(id -un)"; return 1; }; live "$@"; }

pending() {
  local v
  for v in "$@"; do
    [ -n "${!v:-}" ] && continue
    [ "$APPLY" = 1 ] && stop "$v is empty in $ENV_FILE (an earlier window step appends it)"
    printf "PENDING  %s is filled in-window; shown as \${%s} below\n" "$v" "$v"
    printf -v "$v" "\${%s}" "$v"
  done
}

setenv() {
  local q
  printf '  + append %s=%s to %s\n' "$1" "$2" "$ENV_FILE"
  [ "$APPLY" = 1 ] || return 0
  q=$(printf '%s' "$2" | sed "s/'/'\\\\''/g")
  printf "%s='%s'\n" "$1" "$q" >> "$ENV_FILE" || return 1
  export "$1=$2"
}

setenv_secret() {
  local q
  printf '  + append %s (len=%s) to %s\n' "$1" "${#2}" "$ENV_FILE"
  [ "$APPLY" = 1 ] || return 0
  q=$(printf '%s' "$2" | sed "s/'/'\\\\''/g")
  printf "%s='%s'\n" "$1" "$q" >> "$ENV_FILE" || return 1
  export "$1=$2"
}

merge_pr() {
  local repo=$1 pr=$2 st
  printf '  + gh pr ready %s -R %s when draft; require an approval when the branch needs one; gh pr merge %s -R %s --merge\n' "$pr" "$repo" "$pr" "$repo"
  [ "$APPLY" = 1 ] || return 0
  st=$(gh pr view "$pr" -R "$repo" --json state,isDraft,reviewDecision -q '"\(.state) \(.isDraft) \(.reviewDecision)"') || return 1
  case "$st" in
    MERGED*) echo "$repo#$pr is already merged"; return 0 ;;
    OPEN*) ;;
    *) echo "$repo#$pr is $st"; return 1 ;;
  esac
  case "$st" in *" REVIEW_REQUIRED"|*" CHANGES_REQUESTED") echo "$repo#$pr needs an approving review before the merge (collect it before the window)"; return 1 ;; esac
  case "$st" in *" true "*) gh pr ready "$pr" -R "$repo" || return 1 ;; esac
  gh pr merge "$pr" -R "$repo" --merge
}

stalwart_pods() { "${KS[@]}" -n thundermail get pods -l app=stalwart -o json | jq -r '.items[] | select(any(.metadata.ownerReferences[]?; .kind == "ReplicaSet" and (.name | startswith("stalwart-")))) | "pod/" + .metadata.name'; }
listener() { ss -ltnpH "sport = :$1" 2>/dev/null; }
owned_by() { local l; l=$(listener "$1"); [ -n "$l" ] && ! grep -vq "users:((\"$2\"" <<<"$l"; }

legacy_tunnel() {
  local hp=${OLD_STALWART_URL#*://}
  hp=${hp%%/*}
  case "${hp%:*}" in
    127.0.0.1|localhost|'[::1]') ;;
    *) echo "OLD_STALWART_URL is not a loopback URL: credentials not sent"; return 1 ;;
  esac
  owned_by "${hp##*:}" session-manager || { echo "no SSM tunnel owned by $(id -un) on :${hp##*:} (or a foreign listener): credentials not sent"; return 1; }
}

port_forward() {
  local pod port=${STALWART_URL##*:}
  if [ -n "$(listener "$port")" ]; then
    owned_by "$port" kubectl || { echo "a listener on :$port is not a kubectl owned by $(id -un): credentials not sent"; return 1; }
    curl -sf -m 5 -o /dev/null "$STALWART_URL/healthz/live" || { echo "kubectl listener on :$port does not answer"; return 1; }
    echo "reusing the kubectl port-forward on $STALWART_URL"
    PF_OK=1
    return 0
  fi
  pod=$("${KS[@]}" -n thundermail get pod -l app=stalwart --field-selector=status.phase=Running -o name | head -1)
  [ -n "$pod" ] || { echo "no Running Stalwart pod"; return 1; }
  "${KS[@]}" -n thundermail port-forward "$pod" "$port:8080" >/dev/null 2>&1 &
  PF=$!
  until_ok 30 curl -sf -m 5 -o /dev/null "$STALWART_URL/healthz/live" || { echo "port-forward to $pod did not answer"; return 1; }
  echo "port-forward $pod -> $STALWART_URL"
  PF_OK=1
}

cfg_trees() {
  local p
  for p in $CFG_PATHS; do git -C "$REPO_DIR" rev-parse -q --verify "$1:$p" 2>/dev/null || echo none; done | tr '\n' ' '
}

freight_for() {
  local sha=$1 want main list name id
  FREIGHT_FOUND=""
  [ -n "$sha" ] || { echo "freight_for: no commit given"; return 1; }
  timeout 120 git -C "$REPO_DIR" fetch -q "$TMD_URL" "+refs/heads/main:refs/remotes/window/main" "+refs/heads/$PREVIEW_BRANCH:refs/remotes/window/preview" || { echo "git fetch of main and $PREVIEW_BRANCH failed"; return 1; }
  want=$(cfg_trees "$sha")
  case "$want" in *none*) echo "$sha does not resolve $CFG_PATHS"; return 1 ;; esac
  main=$(cfg_trees refs/remotes/window/main)
  [ "$want" = "$main" ] || { echo "main changed $CFG_PATHS after $sha: promoting a Freight for $sha would roll main back"; return 1; }
  list=$("${KH[@]}" -n "$KARGO_PROJECT_CONFIGS" get freight -o json | jq -r --arg wh "${KARGO_WAREHOUSE_CONFIGS:-}" '.items | map(select($wh == "" or .origin.name == $wh)) | sort_by(.metadata.creationTimestamp) | reverse | .[] | "\(.metadata.name) \(.commits[0].id // "")"') || return 1
  while read -r name id; do
    [ -n "$id" ] || continue
    if [ "$(cfg_trees "$id")" = "$want" ]; then FREIGHT_FOUND=$name; break; fi
  done <<<"$list"
  [ -n "$FREIGHT_FOUND" ] || { echo "no Freight from ${KARGO_WAREHOUSE_CONFIGS:-$KARGO_PROJECT_CONFIGS} has a preview commit with the $CFG_PATHS trees of $sha"; return 1; }
  echo "Freight $FREIGHT_FOUND (preview commit $id) matches $sha"
}

finish() {
  if [ "$NEEDS" != 0 ]; then printf '%s %s: %s NEED item(s) above; evidence %s\n' "$KEY" "$(mode)" "$NEEDS" "$LOG"; exit 2; fi
  printf '%s %s: done; evidence %s\n' "$KEY" "$(mode)" "$LOG"
}
