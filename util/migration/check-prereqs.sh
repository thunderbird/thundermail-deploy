#!/usr/bin/env bash
set -uo pipefail
umask 077

usage() {
  cat <<USAGE
usage: check-prereqs.sh [--for KEY]

Prints HAVE or NEED for every tool and credential the tb-stage cutover uses; SKIP marks an optional row left unset.
KEY limits the rows to one task (T1 T2 T3 T5..T11 T10m T10a T13 T14 W1..W8 P1..P3 V1); T0 prints every row.
Values are never printed; exit status is the number of NEED rows, or 64 for a usage error.
USAGE
}

KEYS="T0 T1 T2 T3 T5 T6 T7 T8 T9 T10 T10m T10a T11 T13 T14 W1 W2 W3 W4 W5 W6 W7 W8 P1 P2 P3 V1"
FOR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --for)
      [ $# -ge 2 ] || { echo "--for needs a task key" >&2; usage >&2; exit 64; }
      [ "$2" != T0 ] || { FOR=""; shift 2; continue; }
      case " $KEYS " in *" $2 "*) FOR=$2 ;; *) echo "unknown task key: $2 (one of $KEYS)" >&2; exit 64 ;; esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

WORK_DIR=${WORK_DIR:-$HOME/.local/state/talos}
ENV_FILE=${CUTOVER_ENV:-/dev/shm/talos/cutover-tb-stage.env}
OPERATOR_ENV=${OPERATOR_ENV:-$HOME/.local/state/talos/cutover-operator.env}
KC_STAGE=${KUBECONFIG_STAGE:-$WORK_DIR/kc-stage}
KC_HUB=${KUBECONFIG_HUB:-$WORK_DIR/kc-hub}
KC_DEV=${KUBECONFIG_DEV:-$WORK_DIR/kc-dev}
VENV_PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
REGION=${AWS_REGION:-eu-central-1}
CF=https://api.cloudflare.com/client/v4
. "$(dirname "${BASH_SOURCE[0]}")/spf-decided.sh"

HAVE=0
NEED=0

sm() { aws secretsmanager get-secret-value --profile "$1" --region "${3:-$REGION}" --secret-id "$2" --output json 2>/dev/null | jq -r '.SecretString'; }
sm_exists() { aws secretsmanager describe-secret --profile "$1" --region "${3:-$REGION}" --secret-id "$2" --query Name --output text >/dev/null 2>&1; }
mode600() { [ -f "$1" ] && [ "$(stat -c %a "$1")" = 600 ]; }
from_file() { sed -n "s/^\(export \)\{0,1\}$2=//p" "$1" 2>/dev/null | tail -1 | sed "s/^['\"]//; s/['\"]\$//; s/'\\\\''/'/g"; }
ev() { printf '%s' "${!1:-$(from_file "$ENV_FILE" "$1")}"; }
envfile_has() { [ -n "$(ev "$1")" ]; }
load_operator_env() {
  local f=$1 name val
  [ -e "$f" ] || return 0
  [ "$(stat -c %a "$f")" = 600 ] || { echo "$f must be mode 600" >&2; exit 1; }
  while IFS= read -r name; do
    [ -z "${!name:-}" ] || continue
    val=$(set -a; . "$f" >/dev/null 2>&1; printf '%s' "${!name:-}")
    [ -z "$val" ] || export "$name=$val"
  done < <(sed -n 's/^\(export \)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' "$f")
}
cq() { case $1 in *[$'\n\r']*) return 1 ;; esac; local v=${1//\\/\\\\}; printf '%s' "${v//\"/\\\"}"; }
bearer() { local v; v=$(cq "$1") || return 1; printf 'header = "Authorization: Bearer %s"\n' "$v"; }
cf_status() { curl -s -m 20 -K <(bearer "$1") "$CF/user/tokens/verify" | jq -r '.result.status // "error"'; }
can_i() { kubectl --kubeconfig "$1" auth can-i "$2" "$3" -n "$4" ${5:+--subresource="$5"} 2>/dev/null | grep -qx yes; }
can_all() {
  local kc=$1 bad="" pair; shift
  [ -f "$kc" ] || { echo "kubeconfig missing: $kc"; return 1; }
  for pair in "$@"; do
    set -- $pair; can_i "$kc" "$1" "$2" "$3" "${4:-}" || bad="$bad $1:$2${4:+/$4}@$3"
  done
  [ -z "$bad" ] && echo "all can-i yes" || { echo "can-i no:$bad"; return 1; }
}
tunnel_owned() { local l; l=$(ss -ltnpH "sport = :$1" 2>/dev/null); [ -n "$l" ] && ! printf '%s\n' "$l" | grep -vq 'users:(("session-manager"'; }
legacy_api() {
  local pw
  tunnel_owned 18080 || { echo "no SSM tunnel owned by $(id -un) on :18080 (or a foreign listener); password not sent"; return 1; }
  pw=$(sm mzla-legacy mailstrom/stage/stalwart.postboot.fallback_admin_password)
  [ -n "$pw" ] || { echo "SM fallback_admin_password unreadable"; return 1; }
  curl -sk -m 10 -o /dev/null -w '%{http_code}' -K <(printf 'user = "admin:%s"\n' "$(cq "$pw")") "https://127.0.0.1:18080$1" || true
}

load_operator_env "$OPERATOR_ENV"
c_operator_env() { mode600 "$OPERATOR_ENV" && echo "$OPERATOR_ENV mode 600, $(grep -cE '^(export )?[A-Za-z_][A-Za-z0-9_]*=' "$OPERATOR_ENV") name(s), $(n=0; while IFS= read -r v; do [ -z "${!v:-}" ] || n=$((n + 1)); done < <(sed -n 's/^\(export \)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' "$OPERATOR_ENV"); echo "$n") set" || { echo "$OPERATOR_ENV missing or not mode 600"; return 1; }; }
c_sso() { aws sts get-caller-identity --profile "$1" --query Arn --output text 2>/dev/null | grep -q AWSReservedSSO_AdministratorAccess && echo "AdministratorAccess via $1" || { echo "no session for $1 (aws sso login --sso-session mzla)"; return 1; }; }
c_kube_stage() {
  can_all "$KC_STAGE" 'patch deployments thundermail' 'patch deployments thundermail scale' 'create pods thundermail' 'delete pods thundermail' \
    'create pods thundermail exec' 'create pods thundermail portforward' 'create secrets thundermail' 'delete secrets thundermail' \
    'get secrets thundermail' 'get endpointslices.discovery.k8s.io thundermail' 'get jobs.batch thundermail' 'get externalsecrets.external-secrets.io thundermail' \
    'patch deployments accounts scale' 'create pods accounts exec' 'patch externalsecrets.external-secrets.io accounts' 'get pods accounts log' \
    'patch statefulsets keycloak-customer scale' 'create pods keycloak-customer exec' 'patch externalsecrets.external-secrets.io keycloak-customer' 'create pods keycloak-customer portforward'
}
c_kube_hub() {
  local pairs=('get applications.argoproj.io argocd') ns r
  for ns in tb-accounts tb-stalwart-configs; do
    for r in freights stages warehouses projectconfigs; do pairs+=("get $r.kargo.akuity.io $ns"); done
    pairs+=("create promotions.kargo.akuity.io $ns")
  done
  can_all "$KC_HUB" "${pairs[@]}"
}
c_kube_dev() { can_all "$KC_DEV" 'create pods thundermail' 'patch deployments thundermail scale' 'create pods thundermail portforward'; }
c_neon() {
  command -v neonctl >/dev/null || { echo "neonctl missing"; return 1; }
  mode600 "$HOME/.neon-api.env" || { echo "$HOME/.neon-api.env missing or not mode 600"; return 1; }
  local p id key ok=0 bad="" n
  for p in STALWART_LEGACY KC ACCOUNTS; do
    id=$(ev "NEON_PROJECT_$p")
    [ -n "$id" ] || continue
    key=$(from_file "$HOME/.neon-api.env" "NEON_API_KEY_$p")
    key=${key:-$(from_file "$HOME/.neon-api.env" NEON_API_KEY)}
    if NEON_API_KEY=$key CI=true neonctl projects get "$id" --output json >/dev/null 2>&1; then ok=$((ok + 1)); else bad="$bad $p"; fi
  done
  if [ $((ok)) -gt 0 ] || [ -n "$bad" ]; then
    [ -z "$bad" ] && [ "$ok" = 3 ] && echo "three NEON_PROJECT_* readable with their keys" || { echo "NEON_PROJECT_* readable: $ok of 3${bad:+, failed:$bad}"; return 1; }
    return 0
  fi
  n=$(set -a; . "$HOME/.neon-api.env"; set +a; CI=true neonctl projects list --output json 2>/dev/null | jq '[(if type=="object" then (.projects + (.shared_with_you // [])) else . end)[]] | length' 2>/dev/null)
  [ "${n:-0}" -ge 3 ] && echo "$n project(s) visible (NEON_PROJECT_* not in the env file yet)" || { echo "neonctl sees ${n:-0} project(s), want the three stage projects"; return 1; }
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
  s=$(cf_status "$tok"); [ "$s" = active ] || { echo "token status $s"; return 1; }
  local zones
  zones=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?per_page=50" | jq -r '[.result[].name] | sort | join(",")')
  case ",$zones," in *,stage-thundermail.com,*) ;; *) echo "zones visible: ${zones:-none} (needs stage-thundermail.com and tb.pro)"; return 1 ;; esac
  case ",$zones," in *,tb.pro,*) ;; *) echo "zones visible: ${zones:-none} (needs stage-thundermail.com and tb.pro)"; return 1 ;; esac
  echo "active, stage-thundermail.com and tb.pro visible"
}
c_cf_agent_p2() {
  mode600 "$HOME/.cloudflare.env" || { echo "$HOME/.cloudflare.env missing or not mode 600"; return 1; }
  local tok v zones tbpro rc resp; tok=$(from_file "$HOME/.cloudflare.env" CF_API_TOKEN_AGENT)
  [ -n "$tok" ] || { echo "CF_API_TOKEN_AGENT empty"; return 1; }
  v=$(curl -s -m 20 -K <(bearer "$tok") "$CF/user/tokens/verify")
  [ "$(jq -r '.result.status // "error"' <<<"$v")" = active ] || { echo "token status $(jq -r '.result.status // "error"' <<<"$v")"; return 1; }
  [ -n "$(jq -r '.result.expires_on // empty' <<<"$v")" ] || { echo "P2 token has no expires_on"; return 1; }
  zones=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?per_page=50" | jq -r '[.result[].name] | sort | join(",")')
  case ",$zones," in *,tb.pro,*) ;; *) echo "zones visible: ${zones:-none} (P2 needs tb.pro)"; return 1 ;; esac
  tbpro=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?name=tb.pro" | jq -r '.result[0].id // empty')
  resp=$(curl -s -m 20 -w '\n%{http_code}' -K <(bearer "$tok") "$CF/zones/$tbpro/rulesets/phases/http_request_dynamic_redirect/entrypoint")
  rc=$(tail -1 <<<"$resp")
  if [ "$rc" = 200 ] || { [ "$rc" = 404 ] && sed '$d' <<<"$resp" | jq -e '[.errors[]?.code] | index(10003)' >/dev/null 2>&1; }; then echo "active, expires, tb.pro visible, redirect entrypoint $rc"
  else echo "tb.pro redirect entrypoint $rc"; return 1; fi
}
c_kc_admin_stage() { sm mzla-tb-stage mzla/tb-stage/keycloak-customer-admin-client | jq -Rs -e 'try (fromjson|has("client-secret")) catch false' >/dev/null 2>&1 && echo "SM client-secret key present; token endpoint not probed (needs a port-forward)" || { echo "SM mzla/tb-stage/keycloak-customer-admin-client has no client-secret key"; return 1; }; }
c_kc_admin_legacy() {
  local id sec code
  id=$(sm mzla-legacy accounts/stage/keycloak-admin-client-id); sec=$(sm mzla-legacy accounts/stage/keycloak-admin-client-secret)
  [ -n "$id" ] && [ -n "$sec" ] || { echo "SM accounts/stage/keycloak-admin-client-* unreadable"; return 1; }
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -K <(printf 'data-urlencode = "grant_type=client_credentials"\ndata-urlencode = "client_id=%s"\ndata-urlencode = "client_secret=%s"\n' "$(cq "$id")" "$(cq "$sec")") https://auth-stage.tb.pro/realms/master/protocol/openid-connect/token)
  [ "$code" = 200 ] && echo "token endpoint 200" || { echo "token endpoint $code"; return 1; }
}
c_stalwart_legacy() {
  local code; code=$(legacy_api "/api/principal?types=domain") || { echo "$code"; return 1; }
  [ "$code" = 200 ] && echo "mgmt API 200 with the admin password over the SSM tunnel" || { echo "mgmt API $code"; return 1; }
}
c_stalwart_recovery() { sm mzla-tb-stage mzla/tb-stage/thundermail/stalwart-recovery-admin | jq -Rs -e 'try (fromjson|.recovery_admin|test(":")) catch false' >/dev/null 2>&1 && echo "SM recovery_admin key present; API not probed (needs a port-forward)" || { echo "SM mzla/tb-stage/thundermail/stalwart-recovery-admin has no recovery_admin user:password"; return 1; }; }
c_target_db() { [ -f "$KC_STAGE" ] && kubectl --kubeconfig "$KC_STAGE" -n thundermail get secret stalwart-postgresql-admin-password -o json 2>/dev/null | jq -e '.data|has("STALWART_POSTGRESQL_ADMIN_PASSWORD")' >/dev/null && echo "k8s secret key present; not connected (pg_isready runs from the runner)"; }
c_legacy_db() {
  local bad=""
  for s in mailstrom/stage/stalwart.postboot.postgresql_backend accounts/stage/keycloak-database-password accounts/stage/database-password; do sm_exists mzla-legacy "$s" || bad="$bad $s"; done
  [ -z "$bad" ] && echo "three SM secrets present; not connected (pg_isready runs from the runner)" || { echo "missing:$bad"; return 1; }
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
c_test_mailbox() {
  envfile_has TEST_MAILBOX && envfile_has TEST_MAILBOX_IMAP_HOST && envfile_has TEST_MAILBOX_IMAP_USER && envfile_has TEST_MAILBOX_IMAP_PASSWORD || { echo "TEST_MAILBOX* not in $ENV_FILE"; return 1; }
  local h n; h=$(ev TEST_MAILBOX_IMAP_HOST)
  n=$(printf 'a LOGIN "%s" "%s"\r\nb LOGOUT\r\n' "$(cq "$(ev TEST_MAILBOX_IMAP_USER)")" "$(cq "$(ev TEST_MAILBOX_IMAP_PASSWORD)")" | timeout 20 openssl s_client -quiet -verify_return_error -verify_hostname "$h" -servername "$h" -connect "$h:993" 2>/dev/null | grep -c '^a OK')
  [ "$n" = 1 ] && echo "IMAP LOGIN ok" || { echo "IMAP LOGIN failed"; return 1; }
}
c_test_user() {
  envfile_has TEST_USER || { echo "TEST_USER not in $ENV_FILE"; return 1; }
  local code; code=$(legacy_api "/api/principal/$(ev TEST_USER)") || { echo "$code"; return 1; }
  [ "$code" = 200 ] && echo "TEST_USER principal 200 on legacy" || { echo "TEST_USER principal $code on legacy"; return 1; }
}
c_github() {
  gh auth status >/dev/null 2>&1 || { echo "gh not logged in"; return 1; }
  local bad=""
  for r in platform-infrastructure thundermail-deploy accounts-deploy keycloak-customer-deploy platform-grafana appointment; do
    [ "$(gh api "repos/thunderbird/$r" --jq .permissions.admin 2>/dev/null)" = true ] || bad="$bad $r"
  done
  [ -z "$bad" ] && echo "admin on six repos" || { echo "not admin on:$bad"; return 1; }
}
c_push() {
  local bad="" r
  for r in "$@"; do [ "$(gh api "repos/thunderbird/$r" --jq .permissions.push 2>/dev/null)" = true ] || bad="$bad $r"; done
  [ -z "$bad" ] && echo "push on $*" || { echo "no push on:$bad"; return 1; }
}
c_atlantis_pg() {
  gh api repos/thunderbird/platform-grafana/contents/atlantis.yaml --jq .name >/dev/null 2>&1 || { echo "no atlantis.yaml on platform-grafana main"; return 1; }
  local n; n=$(gh api --paginate "repos/thunderbird/platform-grafana/issues/comments?since=$(date -u -d '30 days ago' +%FT%TZ)&per_page=100" --jq '.[] | select(.user.login == "mzla-atlantis[bot]") | .id' 2>/dev/null | grep -c .)
  [ "${n:-0}" -gt 0 ] && echo "atlantis.yaml present, mzla-atlantis[bot] commented $n time(s) in 30 days" || { echo "no mzla-atlantis[bot] comment on platform-grafana in 30 days"; return 1; }
}
c_appt_workflow() { [ "$(gh api repos/thunderbird/appointment/actions/workflows/deploy-staging.yml --jq .state 2>/dev/null)" = active ] && echo "deploy-staging.yml active" || { echo "deploy-staging.yml not active"; return 1; }; }
c_pr_pair() {
  local repo=thunderbird/$1 pr rev head base
  pr=$(ev "$2"); rev=$(ev "$3")
  [ -n "$pr" ] && [ -n "$rev" ] || { echo "$2/$3 not in $ENV_FILE"; return 1; }
  head=$(gh pr view "$pr" -R "$repo" --json state,headRefName -q 'select(.state == "OPEN") | .headRefName' 2>/dev/null)
  base=$(gh pr view "$rev" -R "$repo" --json state,baseRefName -q 'select(.state == "OPEN") | .baseRefName' 2>/dev/null)
  [ -n "$head" ] && [ "$base" = "$head" ] && echo "$2=#$pr open, $3=#$rev open on its head" || { echo "$2=#$pr head=${head:-not open}, $3=#$rev base=${base:-not open}"; return 1; }
}
c_env_pr() {
  local n st; n=$(ev "$2")
  [ -n "$n" ] || { echo "$2 not in $ENV_FILE"; return 1; }
  st=$(gh pr view "$n" -R "thunderbird/$1" --json state -q .state 2>/dev/null)
  case "$st" in OPEN|MERGED) echo "$2=#$n $st in $1" ;; *) echo "$2=#$n is ${st:-unknown} in $1, not open or merged"; return 1 ;; esac
}
c_recipients() {
  local f; f=$(ev NOTICE_RECIPIENTS_CSV)
  [ "$(grep -c . "$f" 2>/dev/null || echo 0)" -gt 1 ] && echo "$(($(grep -c . "$f") - 1)) recipient row(s) in NOTICE_RECIPIENTS_CSV" || { echo "no recipient rows in NOTICE_RECIPIENTS_CSV (#1547 part 2)"; return 1; }
}
c_git_transport() {
  local bad="" r
  for r in "$@"; do git ls-remote --exit-code "https://github.com/thunderbird/$r.git" HEAD >/dev/null 2>&1 || bad="$bad $r"; done
  [ -z "$bad" ] && echo "git ls-remote over https works for $*" || { echo "git ls-remote failed:$bad (run gh auth setup-git)"; return 1; }
}
c_spf_final() { if [ "$(ev SPF_FINAL_TXT)" = "$SPF_DECIDED_TXT" ]; then echo "SPF_FINAL_TXT equals the #1535 text in the env file"; else echo "SPF_FINAL_TXT in $ENV_FILE is not the #1535 text '$SPF_DECIDED_TXT'"; return 1; fi; }
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
skip() {
  if [ -n "$FOR" ]; then case " $1 " in *" $FOR "*) ;; *) return 0 ;; esac; fi
  printf '%-4s  %-28s  %s\n' SKIP "$2" "$3"
}

printf '%-4s  %-28s  %s\n' STATUS ITEM DETAIL
row "T1 T3 T5 T6 T7 T8 T9 T10 T11 T13 W1 W2 W3 W4 W5 W6 W7 W8 P3 V1" sso-mzla-tb-stage c_sso mzla-tb-stage
row "T3 T7 T8 T10 T11 T13 W2 W3 W4 W6 V1" sso-mzla-legacy c_sso mzla-legacy
row "T2 T6 T10 T11 W1 W5 W7 W8 V1" sso-mzla-shared c_sso mzla-shared
row "T1 T3 T5 T6 T10 W1 W5 W7 W8" sso-mzla-tb-dev c_sso mzla-tb-dev
row "T3 T5 T6 T7 T8 T10 T11 T13 W1 W2 W4 W5 W6 W7 W8 P3 V1" kube-tb-stage c_kube_stage
row "T2 T6 T10 T11 W1 W5 W7 W8 V1" kube-hub c_kube_hub
row "T1 T3 T5 T6 T10 W1 W5 W7 W8" kube-tb-dev c_kube_dev
row "T7 T8 T10 T11 T13 W4" neon-api-key+neonctl c_neon
row "T5 T9 T10 T13 W7" cf-token-shared-external-dns c_cf_shared
row "T9 T10 T13 W7" cf-token-agent c_cf_agent
row "P2" cf-token-agent-p2 c_cf_agent_p2
row "T7 P3" keycloak-admin-tb-stage c_kc_admin_stage
row "T13" keycloak-admin-legacy c_kc_admin_legacy
row "T10 T11 W2 W4" stalwart-admin-legacy c_stalwart_legacy
row "T6 W4 W5 W6 W7" stalwart-recovery-admin c_stalwart_recovery
row "T10 T13 W4" stalwart-target-db-owner c_target_db
row "T7 T8 T10 T11 T13 W4" legacy-db-passwords c_legacy_db
row "T3 T6 W5" accounts-secret-bundle c_accounts_bundle
row "T11 W8" accounts-e2e-user c_e2e_user
row "P1 T10a" paddle-sandbox-key c_paddle
row "T7" t7-pr-and-revert c_pr_pair keycloak-customer-deploy T7_PR T7_REVERT_PR
row "T8" t8-pr-and-revert c_pr_pair accounts-deploy T8_PR T8_REVERT_PR
row "W7" spf-final-txt c_spf_final
row "T14 W1" freeze-pr c_env_pr platform-infrastructure FREEZE_PR
row "T14 W7" flip-pr c_env_pr thundermail-deploy FLIP_PR
if [ -n "$(ev NOTICE_RECIPIENTS_CSV)" ]; then row "T14" notice-recipients-csv c_recipients; else skip "T14" notice-recipients-csv "optional: NOTICE_RECIPIENTS_CSV unset, window notices skipped"; fi
row "T7 T8 W1 W8" git-transport c_git_transport platform-infrastructure accounts-deploy keycloak-customer-deploy thundermail-deploy
row "W7 P3" test-mailbox-external c_test_mailbox
row "W5 W6 W7 P3" test-user-stage c_test_user
row "T0" operator-env-file c_operator_env
row "ALL" github-gh c_github
row "T10m" github-push-pi-pg c_push platform-infrastructure platform-grafana
row "T10m" atlantis-platform-grafana c_atlantis_pg
row "T10a" github-push-appointment c_push appointment
row "T10a" appointment-stage-deploy c_appt_workflow
row "T14 P3" support-channel c_support
row "T10 W3" tool-rclone c_tool rclone
row "T7 T10 W6" tool-podman c_tool podman
row "T10 T11 W2 W4" tool-session-manager-plugin c_tool session-manager-plugin
row "ALL" tool-jq c_tool jq
row "ALL" tool-yq c_tool yq
row "T5 T6 T10 W4 W5" tool-stalwart-cli c_tool stalwart-cli
row "T10 W7" tool-swaks c_tool swaks
row "T6 T10 W4 W5 W6 W7" python-venv c_venv
printf '\nHAVE %d  NEED %d%s\n' "$HAVE" "$NEED" "${FOR:+  (--for $FOR)}"
exit "$NEED"
