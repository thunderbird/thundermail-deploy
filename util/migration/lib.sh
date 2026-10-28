umask 077
MIG_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$MIG_DIR/../.." && pwd)
TS=${TS:-$(date -u +%Y%m%dT%H%M%SZ)}
EVIDENCE_DIR=${EVIDENCE_DIR:-$HOME/.local/state/talos/cutover-tb-stage}
SECRET_DIR=${SECRET_DIR:-/dev/shm/talos/cutover-tb-stage/secret-artifacts}
AWS_REGION=${AWS_REGION:-eu-central-1}
LEGACY_AWS_PROFILE=${LEGACY_AWS_PROFILE:-mzla-legacy}
STAGE_AWS_PROFILE=${STAGE_AWS_PROFILE:-mzla-tb-stage}
CF_API=https://api.cloudflare.com/client/v4
APPLY=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    *) ARGS+=("$a") ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"
mkdir -p "$EVIDENCE_DIR"

die() { echo "$*" >&2; exit 1; }
need() { local v; for v in "$@"; do [ -n "${!v:-}" ] || die "missing $v in the environment: source the cutover env file first"; done; }
mode() { [ "$APPLY" = 1 ] && echo apply || echo dry-run; }
kstage() { need KUBECONFIG_STAGE; kubectl --kubeconfig "$KUBECONFIG_STAGE" "$@"; }
khub() { need KUBECONFIG_HUB; kubectl --kubeconfig "$KUBECONFIG_HUB" "$@"; }
pod() { kstage -n thundermail exec -i runner -c postgres -- "$@"; }
aws_legacy() { aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" "$@"; }
aws_stage() { aws --profile "$STAGE_AWS_PROFILE" --region "$AWS_REGION" "$@"; }
cf_api() { need CF_API_TOKEN_AGENT; curl -sS -m 60 -K <(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "$CF_API_TOKEN_AGENT") "$@"; }
legacy_loopback() {
  local hp=${OLD_STALWART_URL#*://} l
  hp=${hp%%/*}
  case "${hp%:*}" in 127.0.0.1|localhost|'[::1]') ;; *) echo "OLD_STALWART_URL host ${hp%:*} is not loopback: credentials not sent"; return 1 ;; esac
  l=$(ss -ltnpH "sport = :${hp##*:}" 2>/dev/null)
  [ -n "$l" ] || { echo "nothing listens on :${hp##*:}: start the SSM port-forward; credentials not sent"; return 1; }
  ! grep -vq 'users:(("session-manager"' <<<"$l" || { echo "listener on :${hp##*:} is not the SSM tunnel of $(id -un): credentials not sent"; return 1; }
  echo "OLD_STALWART_URL is loopback through the SSM tunnel of $(id -un)"
}
shared_zone_id() {
  local tok
  tok=$(aws_stage secretsmanager get-secret-value --secret-id mzla/tb-stage/cloudflare-external-dns --query SecretString --output text 2>/dev/null | jq -r '.apiToken // empty') || return 1
  [ -n "$tok" ] || return 1
  curl -sS -m 30 -K <(printf 'header = "Authorization: Bearer %s"\n' "$tok") "$CF_API/zones?name=$1" | jq -re '.result[0].id // empty'
}
latest() { ls -1t "$1" 2>/dev/null | head -1; }
