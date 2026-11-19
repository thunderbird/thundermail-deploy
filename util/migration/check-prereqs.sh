#!/usr/bin/env bash
set -uo pipefail
umask 077

IDS="go-W1 go-W2 go-outbound-block go-T7 go-W4 go-W4-read go-W4-reset go-T8 go-W5 go-predns go-mx-flip go-access go-open go-W8 go-P1 go-V2 go-rollback start checks presync backups verify auth-dns pre-cut"
usage() {
  cat <<USAGE
usage: TARGET_ENV=<env> check-prereqs.sh [--for ID[,ID...]]...

Prints HAVE or NEED for every tool and credential the cutover uses; SKIP marks an optional row left unset.
Without --for it prints every row (the inventory of platform-infrastructure#1722).
start holds the rows that can pass before the first window step.
ID is the go row of a GATE or a block: $IDS.
The rows of an id are the ## Prerequisites lines of the issue that holds that GATE: its untagged lines and the lines tagged with the id.
A list prints one block per id. Values are never printed; exit status is the number of NEED rows, or 64 for a usage error.
USAGE
}

FORS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --for)
      [ $# -ge 2 ] || { echo "--for needs a gate id" >&2; usage >&2; exit 64; }
      for id in ${2//,/ }; do
        case " $IDS " in *" $id "*) FORS+=("$id") ;; *) echo "unknown gate id: $id (one of $IDS)" >&2; exit 64 ;; esac
      done
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done
. "$(dirname "${BASH_SOURCE[0]}")/target.sh"

WORK_DIR=${WORK_DIR:-$HOME/.local/state/talos}
ENV_FILE=${CUTOVER_ENV:-$(env_file_default)}
[ ! -f "$ENV_FILE" ] || env_target_ok "$ENV_FILE" || exit 64
OPERATOR_ENV=${OPERATOR_ENV:-$(operator_env_default)}
KC_TARGET=${KUBECONFIG_TARGET:-$WORK_DIR/$TARGET_KUBECONFIG_NAME}
KUBECONFIG_TARGET=$KC_TARGET
KC_HUB=${KUBECONFIG_HUB:-$WORK_DIR/kc-hub}
KC_DEV=${KUBECONFIG_DEV:-$WORK_DIR/kc-dev}
VENV_PY=${VENV_PY:-$HOME/.local/state/talos/venv/bin/python}
REGION=${AWS_REGION:-$TARGET_REGION}
LEGACY=${LEGACY_AWS_PROFILE:-$DEF_LEGACY_AWS_PROFILE}
CF=https://api.cloudflare.com/client/v4

HAVE=0
NEED=0

sm() { aws secretsmanager get-secret-value --profile "$1" --region "${3:-$REGION}" --secret-id "$2" --output json 2>/dev/null | jq -r '.SecretString'; }
sm_exists() { aws secretsmanager describe-secret --profile "$1" --region "${3:-$REGION}" --secret-id "$2" --query Name --output text >/dev/null 2>&1; }
mode600() { [ -f "$1" ] && [ "$(stat -c %a "$1")" = 600 ]; }
from_file() { sed -n "s/^\(export \)\{0,1\}$2=//p" "$1" 2>/dev/null | tail -1 | sed "s/^['\"]//; s/['\"]\$//; s/'\\\\''/'/g"; }
ev() {
  local v rc=0
  if [ -d "$(dirname "$ENV_FILE")" ] && command -v flock >/dev/null 2>&1; then
    v=$( { flock -s -w 60 9 || exit 3; from_file "$ENV_FILE" "$1"; } 9>>"$ENV_FILE.lock" 2>/dev/null ) || rc=$?
    [ "$rc" = 0 ] || { echo "no shared lock on $ENV_FILE.lock: $1 not read" >&2; return 1; }
  else
    v=$(from_file "$ENV_FILE" "$1")
  fi
  printf '%s' "${v:-${!1:-}}"
}
envfile_has() { [ -n "$(ev "$1")" ]; }
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
  pw=$(sm "$LEGACY" "$LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.fallback_admin_password")
  [ -n "$pw" ] || { echo "SM fallback_admin_password unreadable"; return 1; }
  curl -sk -m 10 -o /dev/null -w '%{http_code}' -K <(printf 'user = "admin:%s"\n' "$(cq "$pw")") "https://127.0.0.1:18080$1" || true
}

load_operator_env "$OPERATOR_ENV"
c_operator_env() { mode600 "$OPERATOR_ENV" && echo "$OPERATOR_ENV mode 600, $(grep -cE '^(export )?[A-Za-z_][A-Za-z0-9_]*=' "$OPERATOR_ENV") name(s), $(n=0; while IFS= read -r v; do [ -z "${!v:-}" ] || n=$((n + 1)); done < <(sed -n 's/^\(export \)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' "$OPERATOR_ENV"); echo "$n") set" || { echo "$OPERATOR_ENV missing or not mode 600"; return 1; }; }
c_sso() { aws sts get-caller-identity --profile "$1" --query Arn --output text 2>/dev/null | grep -q AWSReservedSSO_AdministratorAccess && echo "AdministratorAccess via $1" || { echo "no session for $1 (aws sso login --sso-session mzla)"; return 1; }; }
c_kube_target() {
  local apply_pod=()
  [ "$TARGET_ENV" != tb-prod ] || apply_pod=('create pods thundermail attach' 'get pods thundermail log' 'patch pods thundermail')
  can_all "$KC_TARGET" "${apply_pod[@]}" 'patch deployments thundermail' 'patch deployments thundermail scale' 'create pods thundermail' 'delete pods thundermail' \
    'create pods thundermail exec' 'create pods thundermail portforward' 'create secrets thundermail' 'delete secrets thundermail' \
    'get secrets thundermail' 'get endpointslices.discovery.k8s.io thundermail' 'get jobs.batch thundermail' 'get externalsecrets.external-secrets.io thundermail' \
    'patch deployments accounts scale' 'create pods accounts exec' 'patch externalsecrets.external-secrets.io accounts' 'get pods accounts log' \
    'patch statefulsets keycloak-customer scale' 'create pods keycloak-customer exec' 'patch externalsecrets.external-secrets.io keycloak-customer' 'create pods keycloak-customer portforward' \
    'get pods thundermail' 'get statefulsets keycloak-customer' 'get deployments accounts' 'create pods accounts exec'
}
c_kube_hub() {
  local pairs=('get applications.argoproj.io argocd') ns r
  for ns in "$KARGO_PROJECT_ACCOUNTS" "$KARGO_PROJECT_CONFIGS"; do
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
  [ "${n:-0}" -ge 3 ] && echo "$n project(s) visible (NEON_PROJECT_* not in the env file yet)" || { echo "neonctl sees ${n:-0} project(s), want the three $TARGET_ENV projects"; return 1; }
}
c_cf_shared() {
  local tok s z
  tok=$(sm "$TARGET_AWS_PROFILE" "$CF_EXTERNAL_DNS_SECRET" | jq -r '.apiToken // empty')
  [ -n "$tok" ] || { echo "SM $CF_EXTERNAL_DNS_SECRET unreadable"; return 1; }
  s=$(cf_status "$tok"); [ "$s" = active ] || { echo "token status $s"; return 1; }
  z=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?name=$MAIL_ZONE" | jq -r '.result[0].id // empty')
  [ -n "$z" ] && echo "active, zone visible" || { echo "active but Zone:Read missing (operator adds it)"; return 1; }
}
c_cf_agent() {
  mode600 "$CF_TOKEN_FILE" || { echo "$CF_TOKEN_FILE missing or not mode 600"; return 1; }
  local tok s; tok=$(from_file "$CF_TOKEN_FILE" CF_API_TOKEN_AGENT)
  [ -n "$tok" ] || { echo "CF_API_TOKEN_AGENT empty"; return 1; }
  s=$(cf_status "$tok"); [ "$s" = active ] || { echo "token status $s"; return 1; }
  local zones
  zones=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?per_page=50" | jq -r '[.result[].name] | sort | join(",")')
  case ",$zones," in *,"$MAIL_ZONE",*) ;; *) echo "zones visible: ${zones:-none} (needs $MAIL_ZONE and $TBPRO_ZONE)"; return 1 ;; esac
  case ",$zones," in *,"$TBPRO_ZONE",*) ;; *) echo "zones visible: ${zones:-none} (needs $MAIL_ZONE and $TBPRO_ZONE)"; return 1 ;; esac
  echo "active, $MAIL_ZONE and $TBPRO_ZONE visible"
}
c_cf_agent_p2() {
  mode600 "$CF_TOKEN_FILE" || { echo "$CF_TOKEN_FILE missing or not mode 600"; return 1; }
  local tok v zones tbpro rc resp; tok=$(from_file "$CF_TOKEN_FILE" CF_API_TOKEN_AGENT)
  [ -n "$tok" ] || { echo "CF_API_TOKEN_AGENT empty"; return 1; }
  v=$(curl -s -m 20 -K <(bearer "$tok") "$CF/user/tokens/verify")
  [ "$(jq -r '.result.status // "error"' <<<"$v")" = active ] || { echo "token status $(jq -r '.result.status // "error"' <<<"$v")"; return 1; }
  [ -n "$(jq -r '.result.expires_on // empty' <<<"$v")" ] || { echo "token has no expires_on"; return 1; }
  zones=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?per_page=50" | jq -r '[.result[].name] | sort | join(",")')
  case ",$zones," in *,"$TBPRO_ZONE",*) ;; *) echo "zones visible: ${zones:-none} (needs $TBPRO_ZONE)"; return 1 ;; esac
  tbpro=$(curl -s -m 20 -K <(bearer "$tok") "$CF/zones?name=$TBPRO_ZONE" | jq -r '.result[0].id // empty')
  resp=$(curl -s -m 20 -w '\n%{http_code}' -K <(bearer "$tok") "$CF/zones/$tbpro/rulesets/phases/http_request_dynamic_redirect/entrypoint")
  rc=$(tail -1 <<<"$resp")
  if [ "$rc" = 200 ] || { [ "$rc" = 404 ] && sed '$d' <<<"$resp" | jq -e '[.errors[]?.code] | index(10003)' >/dev/null 2>&1; }; then echo "active, expires, $TBPRO_ZONE visible, redirect entrypoint $rc"
  else echo "$TBPRO_ZONE redirect entrypoint $rc"; return 1; fi
}
c_kc_admin_target() { sm "$TARGET_AWS_PROFILE" "$KC_ADMIN_TARGET_SECRET" | jq -Rs -e 'try (fromjson|has("client-secret")) catch false' >/dev/null 2>&1 && echo "SM client-secret key present; token endpoint not probed (needs a port-forward)" || { echo "SM $KC_ADMIN_TARGET_SECRET has no client-secret key"; return 1; }; }
c_kc_admin_legacy() {
  local id sec code
  id=$(sm "$LEGACY" "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-admin-client-id"); sec=$(sm "$LEGACY" "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-admin-client-secret")
  [ -n "$id" ] && [ -n "$sec" ] || { echo "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-admin-client-* unreadable"; return 1; }
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -K <(printf 'data-urlencode = "grant_type=client_credentials"\ndata-urlencode = "client_id=%s"\ndata-urlencode = "client_secret=%s"\n' "$(cq "$id")" "$(cq "$sec")") "https://$LEGACY_AUTH_HOST/realms/master/protocol/openid-connect/token")
  [ "$code" = 200 ] && echo "token endpoint 200" || { echo "token endpoint $code"; return 1; }
}
c_stalwart_legacy() {
  local code; code=$(legacy_api "/api/principal?types=domain&limit=1") || { echo "$code"; return 1; }
  [ "$code" = 200 ] && echo "mgmt API 200 with the admin password over the SSM tunnel" || { echo "mgmt API $code"; return 1; }
}
c_stalwart_recovery() { sm "$TARGET_AWS_PROFILE" "$STALWART_RECOVERY_SECRET" | jq -Rs -e 'try (fromjson|.recovery_admin|test(":")) catch false' >/dev/null 2>&1 && echo "SM recovery_admin key present; API not probed (needs a port-forward)" || { echo "SM $STALWART_RECOVERY_SECRET has no recovery_admin user:password"; return 1; }; }
c_target_db() { [ -f "$KC_TARGET" ] && kubectl --kubeconfig "$KC_TARGET" -n thundermail get secret stalwart-postgresql-admin-password -o json 2>/dev/null | jq -e '.data|has("STALWART_POSTGRESQL_ADMIN_PASSWORD")' >/dev/null && echo "k8s secret key present; not connected (pg_isready runs from the runner)"; }
c_legacy_db() {
  local bad=""
  for s in "$LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend" "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-password" "$LEGACY_ACCOUNTS_SECRET_PREFIX/database-password"; do sm_exists "$LEGACY" "$s" || bad="$bad $s"; done
  [ -z "$bad" ] && echo "three SM secrets present; not connected (pg_isready runs from the runner)" || { echo "missing:$bad"; return 1; }
}
c_accounts_bundle() { sm "$TARGET_AWS_PROFILE" "$TARGET_SECRET_PREFIX/accounts" | jq -Rs -e 'try (fromjson|has("STALWART_WEBHOOK_SECRET")) catch false' >/dev/null 2>&1 && echo "STALWART_WEBHOOK_SECRET key present"; }
c_e2e_user() { sm_exists mzla-shared "$E2E_USER_SECRET" us-east-1 || sm_exists mzla-shared "$E2E_USER_SECRET" && echo "SM secret present"; }
c_paddle() {
  mode600 "$HOME/.paddle.env" || { echo "$HOME/.paddle.env missing or not mode 600"; return 1; }
  local key code; key=$(from_file "$HOME/.paddle.env" PADDLE_API_KEY)
  [ -n "$key" ] || { echo "PADDLE_API_KEY empty"; return 1; }
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -K <(bearer "$key") "$PADDLE_API_URL/notification-settings")
  [ "$code" = 200 ] && echo "$PADDLE_API_URL 200" || { echo "$PADDLE_API_URL $code"; return 1; }
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
c_appt_workflow() { [ "$(gh api "repos/thunderbird/appointment/actions/workflows/$APPT_DEPLOY_WORKFLOW" --jq .state 2>/dev/null)" = active ] && echo "$APPT_DEPLOY_WORKFLOW active" || { echo "$APPT_DEPLOY_WORKFLOW not active"; return 1; }; }
c_pr_pair() {
  local repo=thunderbird/$1 pr rev head base
  pr=$(ev "$2"); rev=$(ev "$3")
  [ -n "$pr" ] && [ -n "$rev" ] || { echo "$2/$3 not in $ENV_FILE"; return 1; }
  head=$(gh pr view "$pr" -R "$repo" --json state,headRefName -q 'select(.state == "OPEN") | .headRefName' 2>/dev/null)
  base=$(gh pr view "$rev" -R "$repo" --json state,baseRefName -q 'select(.state == "OPEN") | .baseRefName' 2>/dev/null)
  [ -n "$head" ] && [ "$base" = "$head" ] && { echo "$2=#$pr open, $3=#$rev open on its head"; return 0; }
  [ -z "$head" ] && [ "$base" = main ] && [ "$(gh pr view "$pr" -R "$repo" --json state -q .state 2>/dev/null)" = MERGED ] && { echo "$2=#$pr merged, $3=#$rev open on main"; return 0; }
  echo "$2=#$pr head=${head:-not open}, $3=#$rev base=${base:-not open}"; return 1
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
c_placeholders() { [ -z "$PLACEHOLDERS" ] && echo "no REPLACE_ME value in targets/$TARGET_ENV.sh or verify/$TARGET_ENV.env" || { echo "fill:$PLACEHOLDERS"; return 1; }; }
c_tool() { command -v "$1" >/dev/null 2>&1 && echo "$(command -v "$1")" || { echo "$1 missing"; return 1; }; }
c_venv() { [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import requests, urllib3' 2>/dev/null && echo "$VENV_PY imports ok" || { echo "venv missing: $VENV_PY"; return 1; }; }
c_checkout() {
  local d sha; d=$(ev TMD_CHECKOUT); sha=$(ev WINDOW_SHA)
  [ -n "$d" ] && [ -n "$sha" ] || { echo "TMD_CHECKOUT or WINDOW_SHA not in $ENV_FILE"; return 1; }
  [ "$(pwd -P)" = "$(cd "$d" 2>/dev/null && pwd -P)" ] || { echo "cwd is not TMD_CHECKOUT"; return 1; }
  [ "$(git rev-parse HEAD 2>/dev/null)" = "$sha" ] || { echo "HEAD is not WINDOW_SHA"; return 1; }
  local st; st=$(git status --porcelain 2>/dev/null) || { echo "git status failed in TMD_CHECKOUT"; return 1; }
  [ -z "$st" ] || { echo "checkout has changes"; return 1; }
  echo "cwd is TMD_CHECKOUT at WINDOW_SHA, clean"
}
c_aws_reads() {
  local p=$1 bad="" c; shift
  for c in "$@"; do aws $c --profile "$p" --region "$REGION" --max-items 1 --output json >/dev/null 2>&1 || bad="$bad ${c// /:}"; done
  [ -z "$bad" ] && echo "$p: $* answer" || { echo "$p denied or failed:$bad"; return 1; }
}
c_legacy_stalwart_pg() { sm "$LEGACY" "$LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend" | jq -e 'has("host") and has("password")' >/dev/null 2>&1 && echo "GetSecretValue ok, host and password keys present" || { echo "GetSecretValue on $LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend failed"; return 1; }; }
c_ghcr() { podman login --get-login ghcr.io >/dev/null 2>&1 && echo "podman logged in to ghcr.io" || { echo "podman has no ghcr.io login"; return 1; }; }
c_restore_files() {
  local dir bad="" f files="cut-legacy-restore.txt t7-restore.txt t8-restore.txt w8-restore.txt"
  dir=$(ev EVIDENCE_DIR); dir=${dir:-$HOME/.local/state/talos/cutover-$TARGET_ENV}
  [ "$TARGET_ENV" != tb-prod ] || files=cut-legacy-restore.txt
  for f in $files; do [ -s "$dir/$f" ] || bad="$bad $f"; done
  [ "$TARGET_ENV" = tb-prod ] || { [ -n "$(ev SECRET_VERSIONS_BEFORE)" ] && [ -s "$(ev SECRET_VERSIONS_BEFORE)" ]; } || bad="$bad SECRET_VERSIONS_BEFORE"
  [ -z "$bad" ] && echo "restore files present" || { echo "missing:$bad"; return 1; }
}
c_clean_clones() {
  local v d bad=""
  for v in PI_DIR KCD_DIR ACD_DIR; do d=$(ev "$v"); [ -n "$d" ] && [ -d "$d/.git" ] && [ -z "$(git -C "$d" status --porcelain 2>/dev/null)" ] || bad="$bad $v"; done
  [ -z "$bad" ] && echo "PI_DIR KCD_DIR ACD_DIR clean clones" || { echo "missing or not clean:$bad"; return 1; }
}

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

R1="checks presync"
LEG="$R1 start backups go-W1 go-W2 go-outbound-block pre-cut go-T7 go-T8 go-W4 go-W4-read auth-dns go-W5 go-predns go-mx-flip go-access go-open go-W8 go-P1 verify go-V2 go-rollback"
HUB="$R1 start go-W1 go-W2 go-outbound-block pre-cut go-T7 go-T8 go-W5 go-predns go-access go-open go-W8 go-P1 verify go-rollback"
rows() {
  [ "$TARGET_ENV" != tb-prod ] || [ -z "$FOR" ] || row "ALL" checkout c_checkout
  [ "$TARGET_ENV" != tb-prod ] || row "ALL" placeholders c_placeholders
  row "ALL" "sso-$TARGET_AWS_PROFILE" c_sso "$TARGET_AWS_PROFILE"
  row "$LEG" "sso-$LEGACY" c_sso "$LEGACY"
  row "$HUB" sso-mzla-shared c_sso mzla-shared
  row "$R1" sso-mzla-tb-dev c_sso mzla-tb-dev
  row "ALL" "kube-$TARGET_ENV" c_kube_target
  row "$HUB" kube-hub c_kube_hub
  row "$R1" kube-tb-dev c_kube_dev
  row "$R1 start backups go-T7 go-T8 go-W4 go-W4-reset auth-dns go-V2 go-rollback" neon-api-key+neonctl c_neon
  [ "$TARGET_ENV" = tb-prod ] || row "checks go-open" cf-token-shared-external-dns c_cf_shared
  row "$R1 start backups auth-dns go-predns go-mx-flip go-access go-open go-W8 verify go-V2 go-rollback" cf-token-agent c_cf_agent
  row "go-access go-open go-W8 go-rollback" cf-token-agent-redirects c_cf_agent_p2
  row "go-T7 go-V2 go-rollback" "keycloak-admin-$TARGET_ENV" c_kc_admin_target
  row "start backups go-rollback" keycloak-admin-legacy c_kc_admin_legacy
  row "$R1 go-W2 go-W4 go-W4-read go-mx-flip go-P1 go-rollback" stalwart-admin-legacy c_stalwart_legacy
  row "start go-W4 go-W4-reset go-W5 go-predns go-open" stalwart-recovery-admin c_stalwart_recovery
  row "$R1 start go-W4 go-W4-reset" stalwart-target-db-owner c_target_db
  row "$R1 start backups go-T7 go-T8 go-W4" legacy-db-passwords c_legacy_db
  [ "$TARGET_ENV" = tb-prod ] || row "start go-W4" legacy-stalwart-pg-secret c_legacy_stalwart_pg
  row "backups" legacy-aws-reads c_aws_reads "$LEGACY" "ecs list-clusters" "logs describe-log-groups" "cloudwatch describe-alarms" "ec2 describe-instances"
  row "backups" "$TARGET_ENV-aws-reads" c_aws_reads "$TARGET_AWS_PROFILE" "ec2 describe-instances"
  row "go-T8 go-predns" accounts-secret-bundle c_accounts_bundle
  if [ "$TARGET_ENV" = tb-prod ]; then skip "go-W8" accounts-e2e-user "tb-prod has no Accounts e2e user (operator decision): no E2E_USER_SECRET to check"; else row "go-W8" accounts-e2e-user c_e2e_user; fi
  row "backups go-access go-open go-P1 go-V2 go-rollback" paddle c_paddle
  row "go-T7" t7-pr-and-revert c_pr_pair keycloak-customer-deploy T7_PR T7_REVERT_PR
  row "go-T8" t8-pr-and-revert c_pr_pair accounts-deploy T8_PR T8_REVERT_PR
  row "go-open" spf-final-txt c_spf_final
  row "checks go-W1" freeze-pr c_env_pr platform-infrastructure FREEZE_PR
  row "checks go-open" flip-pr c_env_pr thundermail-deploy FLIP_PR
  if [ -n "$(ev NOTICE_RECIPIENTS_CSV)" ]; then row "checks" notice-recipients-csv c_recipients; else skip "checks" notice-recipients-csv "optional: NOTICE_RECIPIENTS_CSV unset, window notices skipped"; fi
  row "start go-T7 go-T8 go-W1 go-W8 go-rollback" git-transport c_git_transport platform-infrastructure accounts-deploy keycloak-customer-deploy thundermail-deploy
  case "$(ev TEST_MAILBOX_CHECK)" in
    '') row "start go-open verify" test-mailbox-external c_test_mailbox ;;
    manual) skip "start go-open verify" test-mailbox-external "TEST_MAILBOX_CHECK=manual: the operator reads TEST_MAILBOX by hand; no IMAP login" ;;
    *) echo "TEST_MAILBOX_CHECK=$(ev TEST_MAILBOX_CHECK) in $ENV_FILE: the only value is manual" >&2; exit 64 ;;
  esac
  row "backups go-open verify" "test-user-$TARGET_ENV" c_test_user
  row "checks" operator-env-file c_operator_env
  row "ALL" github-gh c_github
  row "checks" github-push-pi-pg c_push platform-infrastructure platform-grafana
  row "checks" atlantis-platform-grafana c_atlantis_pg
  row "checks" github-push-appointment c_push appointment
  row "checks" appointment-deploy-workflow c_appt_workflow
  row "checks verify" support-channel c_support
  row "go-rollback" rollback-restore-files c_restore_files
  row "go-rollback" clean-clones c_clean_clones
  row "backups" ghcr-login c_ghcr
  skip "backups" log-export-store "to fill: the log export store row of platform-infrastructure#1722"
  row "$R1 start backups go-outbound-block pre-cut go-W4" tool-rclone c_tool rclone
  row "$R1 start backups go-W4 go-predns go-P1" tool-podman c_tool podman
  row "$R1 start go-W2 go-outbound-block go-W4 go-W4-read" tool-session-manager-plugin c_tool session-manager-plugin
  row "ALL" tool-jq c_tool jq
  row "ALL" tool-yq c_tool yq
  row "$R1 start go-W4 go-W4-reset go-W5 go-predns" tool-stalwart-cli c_tool stalwart-cli
  row "$R1 start go-predns go-mx-flip go-access go-open" tool-swaks c_tool swaks
  row "$R1 start backups go-W4 go-W4-read go-W4-reset go-W5 go-predns go-P1" python-venv c_venv
}

printf '%-4s  %-28s  %s\n' STATUS ITEM DETAIL
if [ "${#FORS[@]}" = 0 ]; then
  FOR=""
  rows
  printf '\nHAVE %d  NEED %d\n' "$HAVE" "$NEED"
  exit "$NEED"
fi
TOTAL=0
for FOR in "${FORS[@]}"; do
  HAVE=0 NEED=0
  [ "${#FORS[@]}" = 1 ] || printf '== --for %s\n' "$FOR"
  rows
  printf '\nHAVE %d  NEED %d  (--for %s)\n' "$HAVE" "$NEED" "$FOR"
  TOTAL=$((TOTAL + NEED))
done
[ "${#FORS[@]}" = 1 ] || printf 'NEED %d across %s\n' "$TOTAL" "${FORS[*]}"
exit "$TOTAL"
