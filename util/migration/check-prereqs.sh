#!/usr/bin/env bash
set -uo pipefail
umask 077

usage() {
  cat <<USAGE
usage: check-prereqs.sh [--for KEY]

Prints HAVE or NEED for every tool and credential the tb-stage cutover uses.
KEY limits the rows to one task (T1 T2 T5 T6 T9 T10 T11 T13 T14 W1..W8 P1..P3).
Values are never printed; exit status is the number of NEED rows.
USAGE
}

KEYS="T1 T2 T5 T6 T9 T10 T11 T13 T14 W1 W2 W3 W4 W5 W6 W7 W8 P1 P2 P3"
FOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --for)
      [ $# -ge 2 ] || { echo "--for needs a task key" >&2; usage >&2; exit 2; }
      case " $KEYS " in *" $2 "*) FOR=$2 ;; *) echo "unknown task key: $2 (one of $KEYS)" >&2; exit 2 ;; esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WORK_DIR=${WORK_DIR:-$REPO_DIR/.work}
ENV_FILE=${CUTOVER_ENV:-$HOME/.local/state/talos/cutover-tb-stage.env}
KC_STAGE=${KUBECONFIG_STAGE:-$WORK_DIR/kc-stage}
KC_HUB=${KUBECONFIG_HUB:-$WORK_DIR/kc-hub}
KC_DEV=${KUBECONFIG_DEV:-$WORK_DIR/kc-dev}
VENV_PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
REGION=${AWS_REGION:-eu-central-1}
CF=https://api.cloudflare.com/client/v4

HAVE=0
NEED=0

sm() { aws secretsmanager get-secret-value --profile "$1" --region "${3:-$REGION}" --secret-id "$2" --output json 2>/dev/null | jq -r '.SecretString'; }
sm_exists() { aws secretsmanager describe-secret --profile "$1" --region "${3:-$REGION}" --secret-id "$2" --query Name --output text >/dev/null 2>&1; }
mode600() { [ -f "$1" ] && [ "$(stat -c %a "$1")" = 600 ]; }
from_file() { sed -n "s/^\(export \)\{0,1\}$2=//p" "$1" 2>/dev/null | tail -1 | sed "s/^['\"]//; s/['\"]\$//"; }
envfile_has() { [ -f "$ENV_FILE" ] && [ -n "$(from_file "$ENV_FILE" "$1")" ]; }
bearer() { printf 'header = "Authorization: Bearer %s"\n' "$1"; }
cf_status() { curl -s -m 20 -K <(bearer "$1") "$CF/user/tokens/verify" | jq -r '.result.status // "error"'; }
can_i() { kubectl --kubeconfig "$1" auth can-i "$2" "$3" -n "$4" 2>/dev/null | grep -qx yes; }

c_sso() { aws sts get-caller-identity --profile "$1" --query Arn --output text 2>/dev/null | grep -q AWSReservedSSO_AdministratorAccess && echo "AdministratorAccess via $1" || { echo "no session for $1 (aws sso login --sso-session mzla)"; return 1; }; }
c_kube_stage() {
  [ -f "$KC_STAGE" ] || { echo "kubeconfig missing: $KC_STAGE"; return 1; }
  local bad=""
  for pair in 'create pods thundermail' 'create pods/exec thundermail' 'create secrets thundermail' 'get endpointslices thundermail' 'patch deployments/scale accounts' 'patch statefulsets/scale keycloak-customer'; do
    set -- $pair; can_i "$KC_STAGE" "$1" "$2" "$3" || bad="$bad $1:$2@$3"
  done
  [ -z "$bad" ] && echo "all can-i yes" || { echo "can-i no:$bad"; return 1; }
}
c_kube_hub() {
  [ -f "$KC_HUB" ] || { echo "kubeconfig missing: $KC_HUB"; return 1; }
  can_i "$KC_HUB" get applications.argoproj.io argocd && can_i "$KC_HUB" create promotions.kargo.akuity.io tb-accounts && can_i "$KC_HUB" create promotions.kargo.akuity.io tb-stalwart-configs && echo "applications + promotions yes"
}
c_kube_dev() { [ -f "$KC_DEV" ] || { echo "kubeconfig missing: $KC_DEV"; return 1; }; can_i "$KC_DEV" create pods thundermail && echo "create pods yes"; }
c_neon() {
  command -v neonctl >/dev/null || { echo "neonctl missing"; return 1; }
  mode600 "$HOME/.neon-api.env" || { echo "$HOME/.neon-api.env missing or not mode 600"; return 1; }
  local n; n=$(set -a; . "$HOME/.neon-api.env"; set +a; neonctl projects list --output json 2>/dev/null | jq 'length' 2>/dev/null)
  [ "${n:-0}" -gt 0 ] && echo "$n project(s) visible" || { echo "neonctl projects list failed"; return 1; }
}
c_cf_shared() {
  local tok s z
  tok=$(sm mzla-tb-stage mzla/tb-stage/cloudflare-external-dns | jq -r '.apiToken // empty')
  [ -n "$tok" ] || { echo "SM mzla/tb-stage/cloudflare-external-dns unreadable"; return 1; }
  s=$(cf_status "$tok"); [ "$s" = active ] || { echo "token status $s"; return 1; }
  z=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?name=stage-thundermail.com" | jq -r '.result[0].id // empty')
  [ -n "$z" ] && echo "active, zone visible" || { echo "active but Zone:Read missing (operator adds it)"; return 1; }
}
c_cf_agent() {
  mode600 "$HOME/.cloudflare.env" || { echo "$HOME/.cloudflare.env missing or not mode 600"; return 1; }
  local tok s; tok=$(from_file "$HOME/.cloudflare.env" CF_API_TOKEN_AGENT)
  [ -n "$tok" ] || { echo "CF_API_TOKEN_AGENT empty"; return 1; }
  s=$(cf_status "$tok"); [ "$s" = active ] && echo "active" || { echo "token status $s"; return 1; }
}
c_kc_admin_stage() { [ -n "$(sm mzla-tb-stage mzla/tb-stage/keycloak-customer-admin-client)" ] && echo "SM secret readable" || { echo "SM mzla/tb-stage/keycloak-customer-admin-client unreadable"; return 1; }; }
c_kc_admin_legacy() {
  local id sec code
  id=$(sm mzla-legacy accounts/stage/keycloak-admin-client-id); sec=$(sm mzla-legacy accounts/stage/keycloak-admin-client-secret)
  [ -n "$id" ] && [ -n "$sec" ] || { echo "SM accounts/stage/keycloak-admin-client-* unreadable"; return 1; }
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -K <(printf 'data = "grant_type=client_credentials&client_id=%s&client_secret=%s"\n' "$id" "$sec") https://auth-stage.tb.pro/realms/master/protocol/openid-connect/token)
  [ "$code" = 200 ] && echo "token endpoint 200" || { echo "token endpoint $code"; return 1; }
}
c_stalwart_legacy() {
  local live code
  [ -n "$(sm mzla-legacy mailstrom/stage/stalwart.postboot.fallback_admin_password)" ] || { echo "SM fallback_admin_password unreadable"; return 1; }
  live=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' "https://localhost:18080/healthz/live" 2>/dev/null)
  code=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' "https://localhost:18080/api/principal?types=domain" 2>/dev/null)
  [ "$live" = 200 ] && [ "$code" = 401 ] && echo "tunnel answers (healthz 200, unauthenticated API 401); password readable, not sent" || { echo "healthz $live, API $code (SSM tunnel to localhost:18080 down?)"; return 1; }
}
c_stalwart_recovery() { [ -f "$KC_STAGE" ] && kubectl --kubeconfig "$KC_STAGE" -n thundermail get secret stalwart-recovery-admin -o json 2>/dev/null | jq -e '.data|has("STALWART_USER") and has("STALWART_PASSWORD")' >/dev/null && echo "k8s secret keys present"; }
c_target_db() { [ -f "$KC_STAGE" ] && kubectl --kubeconfig "$KC_STAGE" -n thundermail get secret stalwart-postgresql-admin-password -o json 2>/dev/null | jq -e '.data|has("STALWART_POSTGRESQL_ADMIN_PASSWORD")' >/dev/null && echo "k8s secret key present"; }
c_legacy_db() {
  local bad=""
  for s in mailstrom/stage/stalwart.postboot.postgresql_backend accounts/stage/keycloak-database-password accounts/stage/database-password; do sm_exists mzla-legacy "$s" || bad="$bad $s"; done
  [ -z "$bad" ] && echo "three SM secrets present" || { echo "missing:$bad"; return 1; }
}
c_accounts_bundle() { sm mzla-tb-stage mzla/tb-stage/accounts | jq -Rs -e 'try (fromjson|has("STALWART_WEBHOOK_SECRET")) catch false' >/dev/null 2>&1 && echo "STALWART_WEBHOOK_SECRET key present"; }
c_e2e_user() { sm_exists mzla-shared mzla/shared-services/accounts-e2e-tb-stage us-east-1 || sm_exists mzla-shared mzla/shared-services/accounts-e2e-tb-stage && echo "SM secret present"; }
c_paddle() {
  mode600 "$HOME/.paddle.env" || { echo "$HOME/.paddle.env missing or not mode 600"; return 1; }
  local key code; key=$(from_file "$HOME/.paddle.env" PADDLE_API_KEY)
  [ -n "$key" ] || { echo "PADDLE_API_KEY empty"; return 1; }
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -K <(bearer "$key") https://sandbox-api.paddle.com/notification-settings)
  [ "$code" = 200 ] && echo "sandbox API 200" || { echo "sandbox API $code"; return 1; }
}
c_test_mailbox() { envfile_has TEST_MAILBOX && envfile_has TEST_MAILBOX_IMAP_HOST && envfile_has TEST_MAILBOX_IMAP_USER && envfile_has TEST_MAILBOX_IMAP_PASSWORD && echo "four TEST_MAILBOX* values in the env file" || { echo "TEST_MAILBOX* not in $ENV_FILE"; return 1; }; }
c_test_user() { envfile_has TEST_USER && echo "TEST_USER in the env file" || { echo "TEST_USER not in $ENV_FILE"; return 1; }; }
c_github() {
  gh auth status >/dev/null 2>&1 || { echo "gh not logged in"; return 1; }
  local bad=""
  for r in platform-infrastructure thundermail-deploy accounts-deploy keycloak-customer-deploy platform-grafana appointment; do
    [ "$(gh api "repos/thunderbird/$r" --jq .permissions.admin 2>/dev/null)" = true ] || bad="$bad $r"
  done
  [ -z "$bad" ] && echo "admin on six repos" || { echo "not admin on:$bad"; return 1; }
}
c_support() { envfile_has SUPPORT_CHANNEL && echo "SUPPORT_CHANNEL named in the env file" || { echo "channel not named"; return 1; }; }
c_tool() { command -v "$1" >/dev/null 2>&1 && echo "$(command -v "$1")" || { echo "$1 missing"; return 1; }; }
c_venv() { [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import requests, urllib3' 2>/dev/null && echo "$VENV_PY imports ok" || { echo "venv missing: $VENV_PY"; return 1; }; }

row() {
  local keys=$1 item=$2; shift 2
  if [ -n "$FOR" ]; then case " $keys " in *" $FOR "*|*" ALL "*) ;; *) return 0 ;; esac; fi
  local detail status
  if detail=$("$@" 2>/dev/null); then status=HAVE; HAVE=$((HAVE + 1)); else status=NEED; NEED=$((NEED + 1)); fi
  printf '%-4s  %-28s  %s\n' "$status" "$item" "${detail:-check failed}"
}

printf '%-4s  %-28s  %s\n' STATUS ITEM DETAIL
row "T1 T5 T6 T9 T10 T11 T13 W1 W2 W3 W4 W5 W6 W7 W8 P3" sso-mzla-tb-stage c_sso mzla-tb-stage
row "T10 T11 W2 W3 W6" sso-mzla-legacy c_sso mzla-legacy
row "T2 T6 T11 W1 W5" sso-mzla-shared c_sso mzla-shared
row "T1 T10" sso-mzla-tb-dev c_sso mzla-tb-dev
row "T5 T6 T10 T11 T13 W1 W2 W4 W5 W6 W7 W8 P3" kube-tb-stage c_kube_stage
row "T2 T6 T10 T11 W1 W5 W7 W8" kube-hub c_kube_hub
row "T1 T5 T10 W1 W5 W7 W8" kube-tb-dev c_kube_dev
row "T10 T11 T13 W4" neon-api-key+neonctl c_neon
row "T5 T9 T10 T13 W7" cf-token-shared-external-dns c_cf_shared
row "T9 T10 T13 W7 P2" cf-token-agent c_cf_agent
row "P3" keycloak-admin-tb-stage c_kc_admin_stage
row "T13" keycloak-admin-legacy c_kc_admin_legacy
row "T10 T11 W2 W4" stalwart-admin-legacy c_stalwart_legacy
row "T6 W4 W5 W6 W7" stalwart-recovery-admin c_stalwart_recovery
row "T10 T13 W4" stalwart-target-db-owner c_target_db
row "T10 T11 T13 W4" legacy-db-passwords c_legacy_db
row "T6 W5" accounts-secret-bundle c_accounts_bundle
row "T11 W8" accounts-e2e-user c_e2e_user
row "P1" paddle-sandbox-key c_paddle
row "W7 P3" test-mailbox-external c_test_mailbox
row "W5 W6 W7 P3" test-user-stage c_test_user
row "ALL" github-gh c_github
row "T14 P3" support-channel c_support
row "T10 W3" tool-rclone c_tool rclone
row "T10 W6" tool-podman c_tool podman
row "T10 T11 W2 W4" tool-session-manager-plugin c_tool session-manager-plugin
row "ALL" tool-jq c_tool jq
row "ALL" tool-yq c_tool yq
row "T5 T6 T10 W4 W5" tool-stalwart-cli c_tool stalwart-cli
row "T10 W7" tool-swaks c_tool swaks
row "T6 T10 W4 W5 W6 W7" python-venv c_venv
printf '\nHAVE %d  NEED %d%s\n' "$HAVE" "$NEED" "${FOR:+  (--for $FOR)}"
exit "$NEED"
