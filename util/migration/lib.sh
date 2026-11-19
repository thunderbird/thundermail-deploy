umask 077
MIG_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$MIG_DIR/../.." && pwd)
. "$MIG_DIR/target.sh" || return 64
[ "${CUTOVER_ENV_LOADED:-}" = 1 ] || [ "${BUILDING_ENV:-}" = 1 ] || load_cutover_env
TS=${TS:-$(date -u +%Y%m%dT%H%M%SZ)}
[ "$TARGET_ENV" != tb-prod ] || CUTOVER_ATTEMPT=${CUTOVER_ATTEMPT:-$(cutover_attempt_last)}
EVIDENCE_DIR=${EVIDENCE_DIR:-$(evidence_default)}
SECRET_DIR=${SECRET_DIR:-$(secret_default)}
AWS_REGION=${AWS_REGION:-$TARGET_REGION}
LEGACY_AWS_PROFILE=${LEGACY_AWS_PROFILE:-$DEF_LEGACY_AWS_PROFILE}
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
[ "$APPLY" != 1 ] || no_placeholders || exit 64
mkdir -p "$EVIDENCE_DIR"

die() { echo "$*" >&2; exit 1; }
legacy_stalwart_pg() { [ "$TARGET_ENV" != tb-prod ] || return 1; case " ${RUNNER_LEGACY_PG_KEYS:-stalwart keycloak accounts} " in *" stalwart "*) return 0 ;; esac; return 1; }
need_hint() {
  case $1 in
    STALWART_URL) echo "build-cutover-env.sh writes http://localhost:18081 there; LANE_PF_PORT picks another local port" ;;
    TARGET_NLB_DNS|TARGET_NLB_IPS) echo "it stays empty until the mail load balancer exists; then run build-cutover-env.sh --rebuild" ;;
  esac
}
need() { local v h; for v in "$@"; do [ -n "${!v:-}" ] || { h=$(need_hint "$v"); die "missing $v: not in ${ENV_FILE:-the cutover env file} or the environment; ${h:-run build-cutover-env.sh}"; }; done; }
mode() { [ "$APPLY" = 1 ] && echo apply || echo dry-run; }
ktarget() { need KUBECONFIG_TARGET; kubectl --kubeconfig "$KUBECONFIG_TARGET" "$@"; }
khub() { need KUBECONFIG_HUB; kubectl --kubeconfig "$KUBECONFIG_HUB" "$@"; }
pod() { ktarget -n thundermail exec -i runner -c postgres -- "$@"; }
aws_legacy() { aws --profile "$LEGACY_AWS_PROFILE" --region "$AWS_REGION" "$@"; }
aws_target() { aws --profile "$TARGET_AWS_PROFILE" --region "$AWS_REGION" "$@"; }
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
load_operator_env() {
  local f=$1 name val
  [ -e "$f" ] || return 0
  [ "$(stat -c %a "$f")" = 600 ] || { echo "$f must be mode 600" >&2; exit 1; }
  env_target_ok "$f" || exit 64
  while IFS= read -r name; do
    [ -z "${!name:-}" ] || continue
    val=$(set -a; . "$f" >/dev/null 2>&1; printf '%s' "${!name:-}")
    [ -z "$val" ] || export "$name=$val"
  done < <(sed -n 's/^\(export \)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' "$f")
}
shared_zone_id() {
  local tok
  tok=$(aws_target secretsmanager get-secret-value --secret-id "$CF_EXTERNAL_DNS_SECRET" --query SecretString --output text 2>/dev/null | jq -r '.apiToken // empty') || return 1
  [ -n "$tok" ] || return 1
  curl -sS -m 30 -K <(printf 'header = "Authorization: Bearer %s"\n' "$tok") "$CF_API/zones?name=$1" | jq -re '.result[0].id // empty'
}
latest() { ls -1t "$1" 2>/dev/null | head -1; }
