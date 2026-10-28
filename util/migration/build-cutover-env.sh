#!/usr/bin/env bash
set -euo pipefail
MODE=build
for a in "$@"; do
  case "$a" in
    --check) MODE=check ;;
    -h|--help) echo 'usage: build-cutover-env.sh [--check]'; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ENV_FILE=${CUTOVER_ENV:-/dev/shm/talos/cutover-tb-stage.env}
OPERATOR_ENV=${OPERATOR_ENV:-$HOME/.local/state/talos/cutover-operator.env}
load_operator_env "$OPERATOR_ENV"
VALUES=$REPO_DIR/overlays/tb-stage-thundermail/stalwart-configs/config/values.yaml
NEON_API=https://console.neon.tech/api/v2
CARRY="FREEZE_SHA THAW_PR T7_ENV_SHA T8_ENV_SHA T6_SHA FREIGHT STALWART_ADMIN_ACCOUNT_ID CURRENT_ACCOUNTS_FREIGHT THAW_SHA T7_START T8_START DECISIONS_COMMENT_URL CUT_ECS_END TEST_SENDER_APP_PASSWORD PADDLE_NOTIFICATION_ID"
OPERATOR_VARS="FREEZE_PR T6_PR FLIP_PR APPOINTMENT_PR SPF_APEX_TXT SPF_MAIL_TXT SPF_FINAL_TXT TEST_MAILBOX TEST_MAILBOX_IMAP_HOST TEST_MAILBOX_IMAP_USER TEST_MAILBOX_IMAP_PASSWORD TEST_USER SUPPORT_CHANNEL"
SECRET_VARS="STALWART_TARGET_PG_PASSWORD STALWART_PASSWORD OLD_STALWART_PASSWORD STALWART_LEGACY_PG_PASSWORD ACCOUNTS_LEGACY_PG_PASSWORD KC_LEGACY_PG_PASSWORD KC_LEGACY_ADMIN_CLIENT_SECRET CF_API_TOKEN_AGENT PADDLE_API_KEY TEST_MAILBOX_IMAP_PASSWORD"

try() { ( "$@" ) 2>/dev/null || true; }
prev() { [ -f "$ENV_FILE" ] || return 0; ( set +eu; . "$ENV_FILE" >/dev/null 2>&1; printf '%s' "${!1:-}" ); }
op() { printf '%s' "${!1:-$(prev "$1")}"; }
from_file() { [ -f "$1" ] && sed -n "s/^\(export \)\{0,1\}$2=//p" "$1" | head -1 | sed "s/^['\"]//; s/['\"]\$//" || true; }
bearer() { printf 'header = "Authorization: Bearer %s"\n' "$1"; }
sm_legacy() { aws_legacy secretsmanager get-secret-value --secret-id "$1" --output json | jq -r '.SecretString'; }
sm_stage() { aws_stage secretsmanager get-secret-value --secret-id "$1" --output json | jq -r '.SecretString'; }
k8s_secret() { kstage -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }
direct() { sed 's/-pooler//'; }
cf_zone() { [ -n "${CF_TOKEN:-}" ] || return 0; curl -sS -m 30 -K <(bearer "$CF_TOKEN") "$CF_API/zones?name=$1" | jq -r '.result[0].id // empty'; }
pr_by_head() { gh pr list -R "thunderbird/$1" --state open --json number,headRefName --jq "[.[] | select(.headRefName | test(\"(^|/)$2\$\"))] | if length == 1 then .[0].number else empty end"; }
neon_get() { curl -sS -m 60 -K <(bearer "${KEY:-$NEON_KEY}") "$NEON_API/$1"; }
neon_lookup() {
  local ep=${2%%.*} pid bid KEY pids
  KEY=$(from_file "$HOME/.neon-api.env" "NEON_API_KEY_$1")
  KEY=${KEY:-${NEON_KEY:-}}
  [ -n "$KEY" ] || return 0
  pids=$(op "NEON_PROJECT_$1")
  for pid in ${pids:-$NEON_PROJECTS}; do
    [ -n "${NEON_EP_CACHE[$pid]:-}" ] || NEON_EP_CACHE[$pid]=$(try neon_get "projects/$pid/endpoints" | jq -c '[.endpoints[]? | {id, branch_id}]')
    bid=$(jq -r --arg ep "$ep" '.[] | select(.id == $ep) | .branch_id' <<<"${NEON_EP_CACHE[$pid]:-[]}")
    if [ -n "$bid" ]; then
      printf '%s\t%s\n' "$pid" "$(try neon_get "projects/$pid/branches/$bid" | jq -r '.branch.name // empty')"
      return 0
    fi
  done
}

build() {
  KUBECONFIG_STAGE=$(realpath -m "${KUBECONFIG_STAGE:-$HOME/.local/state/talos/kc-stage}")
  KUBECONFIG_HUB=$(realpath -m "${KUBECONFIG_HUB:-$HOME/.local/state/talos/kc-hub}")
  KUBECONFIG_DEV=$(realpath -m "${KUBECONFIG_DEV:-$HOME/.local/state/talos/kc-dev}")
  mkdir -p "$(dirname "$ENV_FILE")" "$EVIDENCE_DIR" "$SECRET_DIR"
  env_path_ok "$ENV_FILE" || die "refusing $ENV_FILE: it or its directory is a symlink, not owned by $(id -un), or writable by group or others"
  local count=0 missing=0
  tmp=$(mktemp "$(dirname "$ENV_FILE")/.cutover-tb-stage.XXXXXX")
  trap 'rm -f "$tmp"' EXIT
  put() {
    local name=$1 value=$2 source=$3 q
    q=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
    printf '# source: %s\n%s=%s\n' "$source" "$name" "'$q'" >> "$tmp"
    count=$((count + 1))
    if [ -n "$value" ]; then printf '%-30s len=%s\n' "$name" "${#value}"; else printf '%-30s len=0 NEED\n' "$name"; missing=$((missing + 1)); fi
  }
  printf '# built %s by util/migration/build-cutover-env.sh; mode 600; never commit\n' "$(date -u +%FT%TZ)" > "$tmp"

  local cfg; cfg=$(try kstage -n thundermail get cm stalwart-config -o jsonpath='{.data.config\.json}')
  put STALWART_TARGET_PG_HOST "$(jq -r '.host // empty' <<<"${cfg:-{\}}" | direct)" "kubectl -n thundermail get cm stalwart-config (config.json host, -pooler stripped)"
  put STALWART_TARGET_PG_DB "$(jq -r '.database // empty' <<<"${cfg:-{\}}")" "kubectl -n thundermail get cm stalwart-config (config.json database)"
  put STALWART_TARGET_PG_USER "$(jq -r '.authUsername // empty' <<<"${cfg:-{\}}")" "kubectl -n thundermail get cm stalwart-config (config.json authUsername)"
  put STALWART_TARGET_PG_PASSWORD "$(try k8s_secret thundermail stalwart-postgresql-admin-password STALWART_POSTGRESQL_ADMIN_PASSWORD)" "kubectl -n thundermail get secret stalwart-postgresql-admin-password key STALWART_POSTGRESQL_ADMIN_PASSWORD"
  put STALWART_USER "$(try k8s_secret thundermail stalwart-recovery-admin STALWART_USER)" "kubectl -n thundermail get secret stalwart-recovery-admin key STALWART_USER"
  put STALWART_PASSWORD "$(try k8s_secret thundermail stalwart-recovery-admin STALWART_PASSWORD)" "kubectl -n thundermail get secret stalwart-recovery-admin key STALWART_PASSWORD"
  put STALWART_URL http://localhost:18081 "port-forward to the Stalwart pod, 18081:8080"
  put OLD_STALWART_URL https://127.0.0.1:18080 "SSM port-forward to LEGACY_MGMT_INSTANCE 8080; management listener is implicit TLS"
  put OLD_STALWART_USER admin "legacy fallback admin"
  put OLD_STALWART_PASSWORD "$(try sm_legacy mailstrom/stage/stalwart.postboot.fallback_admin_password)" "aws secretsmanager get-secret-value --profile mzla-legacy mailstrom/stage/stalwart.postboot.fallback_admin_password"

  local lpg; lpg=$(try sm_legacy mailstrom/stage/stalwart.postboot.postgresql_backend)
  put STALWART_LEGACY_PG_HOST "$(jq -r '.host // empty' <<<"${lpg:-{\}}" | direct)" "SM mailstrom/stage/stalwart.postboot.postgresql_backend .host (-pooler stripped)"
  put STALWART_LEGACY_PG_DB "$(jq -r '.database // empty' <<<"${lpg:-{\}}")" "SM mailstrom/stage/stalwart.postboot.postgresql_backend .database"
  put STALWART_LEGACY_PG_USER "$(jq -r '.user // empty' <<<"${lpg:-{\}}")" "SM mailstrom/stage/stalwart.postboot.postgresql_backend .user"
  put STALWART_LEGACY_PG_PASSWORD "$(jq -r '.password // empty' <<<"${lpg:-{\}}")" "SM mailstrom/stage/stalwart.postboot.postgresql_backend .password"
  put ACCOUNTS_LEGACY_PG_HOST "$(try sm_legacy accounts/stage/database-host | direct)" "SM accounts/stage/database-host (-pooler stripped)"
  put ACCOUNTS_LEGACY_PG_NAME "$(try sm_legacy accounts/stage/database-name)" "SM accounts/stage/database-name"
  put ACCOUNTS_LEGACY_PG_USER "$(try sm_legacy accounts/stage/database-user)" "SM accounts/stage/database-user"
  put ACCOUNTS_LEGACY_PG_PASSWORD "$(try sm_legacy accounts/stage/database-password)" "SM accounts/stage/database-password"
  put KC_LEGACY_PG_HOST "$(try sm_legacy accounts/stage/keycloak-database-host | direct)" "SM accounts/stage/keycloak-database-host (-pooler stripped)"
  put KC_LEGACY_PG_DB "$(try sm_legacy accounts/stage/keycloak-database-name)" "SM accounts/stage/keycloak-database-name"
  put KC_LEGACY_PG_USER "$(try sm_legacy accounts/stage/keycloak-database-user)" "SM accounts/stage/keycloak-database-user"
  put KC_LEGACY_PG_PASSWORD "$(try sm_legacy accounts/stage/keycloak-database-password)" "SM accounts/stage/keycloak-database-password"
  put KC_LEGACY_ADMIN_CLIENT_ID "$(try sm_legacy accounts/stage/keycloak-admin-client-id)" "SM accounts/stage/keycloak-admin-client-id"
  put KC_LEGACY_ADMIN_CLIENT_SECRET "$(try sm_legacy accounts/stage/keycloak-admin-client-secret)" "SM accounts/stage/keycloak-admin-client-secret"
  put KC_LEGACY_URL https://auth-stage.tb.pro "legacy stage Keycloak"

  NEON_KEY=$(from_file "$HOME/.neon-api.env" NEON_API_KEY)
  local org; org=$(from_file "$HOME/.neon-api.env" NEON_ORG_ID)
  NEON_PROJECTS=""
  declare -A NEON_EP_CACHE=()
  [ -n "$NEON_KEY" ] && NEON_PROJECTS=$(try neon_get "projects?limit=400${org:+&org_id=$org}" | jq -r '.projects[]?.id' | tr '\n' ' ')
  local r
  r=$(neon_lookup STALWART_LEGACY "$(jq -r '.host // empty' <<<"${lpg:-{\}}" | direct)")
  put NEON_PROJECT_STALWART_LEGACY "$(op NEON_PROJECT_STALWART_LEGACY)$([ -n "$(op NEON_PROJECT_STALWART_LEGACY)" ] || cut -f1 <<<"$r")" "NEON_PROJECT_STALWART_LEGACY in the environment, else Neon API: project whose endpoint id is the STALWART_LEGACY_PG_HOST prefix"
  put NEON_LEGACY_STALWART_BRANCH "$(cut -f2 <<<"$r")" "Neon API: branch of that endpoint"
  r=$(neon_lookup KC "$(try sm_legacy accounts/stage/keycloak-database-host | direct)")
  put NEON_PROJECT_KC "$(op NEON_PROJECT_KC)$([ -n "$(op NEON_PROJECT_KC)" ] || cut -f1 <<<"$r")" "NEON_PROJECT_KC in the environment, else Neon API: project whose endpoint id is the KC_LEGACY_PG_HOST prefix"
  r=$(neon_lookup ACCOUNTS "$(try sm_legacy accounts/stage/database-host | direct)")
  put NEON_PROJECT_ACCOUNTS "$(op NEON_PROJECT_ACCOUNTS)$([ -n "$(op NEON_PROJECT_ACCOUNTS)" ] || cut -f1 <<<"$r")" "NEON_PROJECT_ACCOUNTS in the environment, else Neon API: project whose endpoint id is the ACCOUNTS_LEGACY_PG_HOST prefix"

  put LEGACY_BLOB_BUCKET "$(try aws_legacy s3api list-buckets --query "Buckets[?contains(Name,'stage') && ends_with(Name,'-stalwart-s3-store')].Name | [0]" --output text | grep -v '^None$' || true)" "aws s3api list-buckets --profile mzla-legacy (name *stage*-stalwart-s3-store)"
  put TARGET_BLOB_BUCKET "$(yq -r '.blobStoreBucket // ""' "$VALUES")" "overlays/tb-stage-thundermail/stalwart-configs/config/values.yaml blobStoreBucket"
  put REDIS_URL "$(yq -r '.inMemoryStoreURL // ""' "$VALUES")" "overlays/tb-stage-thundermail/stalwart-configs/config/values.yaml inMemoryStoreURL"

  put LEGACY_MGMT_INSTANCE "$(try aws_legacy ec2 describe-instances --filters 'Name=tag:Name,Values=*mailstrom-stage*-50' Name=instance-state-name,Values=running --query 'Reservations[0].Instances[0].InstanceId' --output text | grep -v '^None$' || true)" "aws ec2 describe-instances --profile mzla-legacy Name=*mailstrom-stage*-50 running"
  local lb; lb=$(try aws_legacy elbv2 describe-load-balancers --names mailstrom-stage-pub --query 'LoadBalancers[0].[LoadBalancerArn,SecurityGroups[0]]' --output text)
  put LEGACY_PUB_LB_ARN "$(cut -f1 <<<"$lb")" "aws elbv2 describe-load-balancers --profile mzla-legacy --names mailstrom-stage-pub"
  local sg; sg=$(cut -f2 <<<"$lb")
  put LEGACY_PUB_SG "$sg" "aws elbv2 describe-load-balancers --profile mzla-legacy --names mailstrom-stage-pub SecurityGroups[0]"
  put LEGACY_PUB_SG_RULE_IDS "$([ -n "$sg" ] && try aws_legacy ec2 describe-security-group-rules --filters "Name=group-id,Values=$sg" --query 'SecurityGroupRules[?IsEgress==`false`].SecurityGroupRuleId' --output text | tr '\t' '\n' | sort | tr '\n' ' ' | sed 's/ $//')" "aws ec2 describe-security-group-rules --profile mzla-legacy group-id=LEGACY_PUB_SG, ingress"
  local clusters targets=""
  clusters=$(try aws_legacy ecs list-clusters --query 'clusterArns[]' --output text | tr '\t' '\n' | sed 's|.*/||' | { grep '^accounts-stage' || true; } | sort)
  for c in $clusters; do targets="$targets $(try aws_legacy ecs list-services --cluster "$c" --query 'serviceArns[]' --output text | tr '\t' '\n' | sed "s|.*/||; s|^|$c/|" | sort | tr '\n' ' ')"; done
  targets=$(echo $targets)
  put LEGACY_ECS_TARGETS "$targets" "aws ecs list-clusters/list-services --profile mzla-legacy, clusters accounts-stage*"
  local rids=""
  for t in $targets; do rids="$rids service/$t"; done
  put LEGACY_ASG_RESOURCE_IDS "$([ -n "$rids" ] && try aws_legacy application-autoscaling describe-scalable-targets --service-namespace ecs --resource-ids $rids --query 'ScalableTargets[].ResourceId' --output text | tr '\t' ' ')" "aws application-autoscaling describe-scalable-targets --profile mzla-legacy for LEGACY_ECS_TARGETS"

  put ARGO_APPS "tb-stage-app-of-apps tb-stage-thundermail tb-stage-accounts tb-stage-keycloak-customer" "platform-infrastructure argocd/ app names"
  put KARGO_PROJECT_ACCOUNTS tb-accounts "Kargo project"
  put KARGO_STAGE_ACCOUNTS tb-stage "Kargo stage"
  put KARGO_PROJECT_CONFIGS tb-stalwart-configs "Kargo project"
  put KARGO_STAGE_CONFIGS deploy-thundermail-tb-stage01 "Kargo stage"
  put KARGO_WAREHOUSE_CONFIGS deploy-thundermail-tb-stage01-git "Kargo warehouse"
  put FREEZE_PR "$(op FREEZE_PR)$([ -z "$(op FREEZE_PR)" ] && try gh pr list -R thunderbird/platform-infrastructure --head freeze-tb-stage --state open --json number --jq '.[0].number')" "gh pr list -R thunderbird/platform-infrastructure --head freeze-tb-stage, or FREEZE_PR in the environment"
  put T6_PR "$(op T6_PR)" "operator: TMD values.yaml PR number (T6_PR in the environment)"
  for d in T7_PR:keycloak-customer-deploy:tbstage-t7 T7_REVERT_PR:keycloak-customer-deploy:tbstage-t7-revert T8_PR:accounts-deploy:tbstage-t8 T8_REVERT_PR:accounts-deploy:tbstage-t8-revert; do
    local v repo head n
    IFS=: read -r v repo head <<<"$d"
    n=$(op "$v")
    [ -n "$n" ] || n=$(try pr_by_head "$repo" "$head")
    put "$v" "$n" "$v in the environment, else the one open thunderbird/$repo PR with head */$head (gh pr list)"
  done
  put FLIP_PR "$(op FLIP_PR)" "operator: TMD dnsManagement Automatic PR number (FLIP_PR in the environment)"
  put APPOINTMENT_PR "$(op APPOINTMENT_PR)" "operator: appointment stage PR number (APPOINTMENT_PR in the environment)"
  for d in PI_DIR:platform-infrastructure KCD_DIR:keycloak-customer-deploy ACD_DIR:accounts-deploy APPOINTMENT_DIR:appointment TMD_DIR:thundermail-deploy; do
    local v=${d%%:*} repo=${d##*:} path
    path=${!v:-$HOME/src/tb/$repo}
    [ -d "$path/.git" ] || path=""
    put "$v" "$path" "local checkout of thunderbird/$repo ($v in the environment, default \$HOME/src/tb/$repo)"
  done

  put AWS_REGION "$AWS_REGION" "eu-central-1"
  CF_TOKEN=$(from_file "$HOME/.cloudflare.env" CF_API_TOKEN_AGENT)
  put CF_API_TOKEN_AGENT "$CF_TOKEN" "\$HOME/.cloudflare.env CF_API_TOKEN_AGENT"
  local zid; zid=$(try cf_zone stage-thundermail.com)
  [ -n "$zid" ] || zid=$(try shared_zone_id stage-thundermail.com)
  put CF_ZONE_ID "$zid" "GET /zones?name=stage-thundermail.com with CF_API_TOKEN_AGENT, else with the shared external-dns token (read-only)"
  put CF_ZONE_ID_TBPRO "$(try cf_zone tb.pro)" "GET /zones?name=tb.pro"
  put CF_ZONE_ID_DEVTM "$(try cf_zone dev-thundermail.com)" "GET /zones?name=dev-thundermail.com"
  put PADDLE_API_KEY "$(from_file "$HOME/.paddle.env" PADDLE_API_KEY)" "\$HOME/.paddle.env PADDLE_API_KEY"
  put ACCOUNTS_WEBHOOK_URL https://accounts.stage-thundermail.com/api/v1/telemetry/stalwart/webhook/ "tb-stage Accounts telemetry webhook"
  local nlb; nlb=$(try kstage -n thundermail get svc nginx -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
  [ -n "$nlb" ] || nlb=$(try aws_stage elbv2 describe-load-balancers --names stalwart-nginx-thundermail --query 'LoadBalancers[0].DNSName' --output text | grep -v '^None$' || true)
  put TARGET_NLB_DNS "$nlb" "kubectl -n thundermail get svc nginx .status.loadBalancer.ingress[0].hostname, resolved live on every build (else aws elbv2 --names stalwart-nginx-thundermail)"
  put TEST_MAILBOX "$(op TEST_MAILBOX)" "operator: external test mailbox; must be TEST_USER's Keycloak email (the backup address)"
  put TEST_MAILBOX_IMAP_HOST "$(op TEST_MAILBOX_IMAP_HOST)" "operator: external test mailbox IMAP host"
  put TEST_MAILBOX_IMAP_USER "$(op TEST_MAILBOX_IMAP_USER)" "operator: external test mailbox IMAP user"
  put TEST_MAILBOX_IMAP_PASSWORD "$(op TEST_MAILBOX_IMAP_PASSWORD)" "operator: external test mailbox IMAP password"
  put TEST_USER "$(op TEST_USER)" "operator: migrated stage mailbox the operator owns"
  put TEST_SENDER "$(op TEST_USER)" "same as TEST_USER"
  put SPF_APEX_TXT "$(op SPF_APEX_TXT)" "operator: apex SPF text decided in the SPF spike"
  put SPF_MAIL_TXT "$(op SPF_MAIL_TXT)" "operator: mail. SPF text decided in the SPF spike"
  put SPF_FINAL_TXT "$(op SPF_FINAL_TXT)" "operator: spf. TXT from the #1535 decision (SPF_FINAL_TXT in the environment)"
  put SUPPORT_CHANNEL "$(op SUPPORT_CHANNEL)" "operator: support announcement channel"
  for v in KUBECONFIG_STAGE KUBECONFIG_HUB KUBECONFIG_DEV; do
    put "$v" "${!v}" "aws eks update-kubeconfig --kubeconfig ${!v} ($v in the environment)"
    [ -f "${!v}" ] || { printf '%-30s file missing\n' "$v"; missing=$((missing + 1)); }
  done
  put EVIDENCE_DIR "$EVIDENCE_DIR" "evidence directory"
  local tsv; tsv=$(op TIMING_TSV); put TIMING_TSV "${tsv:-$EVIDENCE_DIR/timing-tb-stage.tsv}" "TIMING_TSV in the environment or the previous file, else \$EVIDENCE_DIR/timing-tb-stage.tsv"
  put SECRET_DIR "$SECRET_DIR" "tmpfs directory for secret artifacts"
  for side in LEG:mzla-legacy TGT:mzla-tb-stage; do
    local p=${side%%:*}
    put "RCLONE_CONFIG_${p}_TYPE" s3 rclone
    put "RCLONE_CONFIG_${p}_PROVIDER" AWS rclone
    put "RCLONE_CONFIG_${p}_ENV_AUTH" true rclone
    put "RCLONE_CONFIG_${p}_REGION" "$AWS_REGION" rclone
    put "RCLONE_CONFIG_${p}_PROFILE" "${side##*:}" rclone
  done
  for name in $CARRY; do
    local v; v=$(op "$name")
    [ -n "$v" ] || continue
    printf '# carried over from the previous env file\n%s=%s\n' "$name" "'$(printf '%s' "$v" | sed "s/'/'\\\\''/g")'" >> "$tmp"
    printf '%-30s len=%s (carried)\n' "$name" "${#v}"
  done
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
  printf 'written %s (mode %s) vars=%s missing=%s\n' "$ENV_FILE" "$(stat -c %a "$ENV_FILE")" "$count" "$missing"
  old=$HOME/.local/state/talos/cutover-tb-stage.env
  [ "$old" = "$ENV_FILE" ] || [ ! -e "$old" ] || { printf 'old env file %s is on persistent disk: delete it\n' "$old"; missing=$((missing + 1)); }
  [ "$missing" = 0 ]
}

check() {
  env_path_ok "$ENV_FILE" || die "refusing $ENV_FILE: it or its directory is a symlink, not owned by $(id -un), or writable by group or others"
  [ -f "$ENV_FILE" ] || die "$ENV_FILE missing: run build-cutover-env.sh first"
  set -a; . "$ENV_FILE"; set +a
  . "$MIG_DIR/timing.sh"
  local start fails=0 secret_ids=${REPOINT_SECRET_IDS:-mzla/tb-stage/keycloak-customer-db mzla/tb-stage/accounts-db}
  start=$(date -u +%FT%TZ)
  chk() {
    local name=$1 out; shift
    if out=$("$@" 2>&1); then printf 'PASS  %-32s %s\n' "$name" "$(tail -1 <<<"$out" | cut -c1-110)"
    else fails=$((fails + 1)); printf 'FAIL  %-32s %s\n' "$name" "$(tail -1 <<<"$out" | cut -c1-110)"; fi
  }
  operator_values() { local v bad=""; for v in $OPERATOR_VARS; do [ -n "${!v:-}" ] || bad="$bad $v"; done; [ -z "$bad" ] && echo "all operator values set" || { echo "empty:$bad"; return 1; }; }
  nonempty() { local v bad=""; for v in $SECRET_VARS; do [ -n "${!v:-}" ] || bad="$bad $v"; done; [ -z "$bad" ] && echo "all set" || { echo "empty:$bad"; return 1; }; }
  dirs() {
    local bad="" d
    for d in EVIDENCE_DIR SECRET_DIR; do [ "$(stat -c %a "${!d}" 2>/dev/null)" = 700 ] || bad="$bad $d"; done
    for d in KUBECONFIG_STAGE KUBECONFIG_HUB KUBECONFIG_DEV; do [ -f "${!d}" ] || bad="$bad $d"; done
    [ -z "$bad" ] && echo "dirs 700, three kubeconfigs present" || { echo "missing or wrong mode:$bad"; return 1; }
  }
  runner() { [ "$(kstage -n thundermail get pod runner -o jsonpath='{.status.phase}')" = Running ] && echo Running; }
  code() { curl -sk -m 15 -o /dev/null -w '%{http_code}' "$@"; }
  http200() { local c; c=$(code "$@"); [ "$c" = 200 ] && echo "HTTP 200" || { echo "HTTP $c"; return 1; }; }
  shared_scope() {
    local tok s z; tok=$(sm_stage mzla/tb-stage/cloudflare-external-dns | jq -r '.apiToken // empty')
    s=$(curl -s -m 20 -K <(bearer "$tok") "$CF_API/user/tokens/verify" | jq -r '.result.status // "error"')
    z=$(curl -s -m 20 -K <(bearer "$tok") "$CF_API/zones?name=stage-thundermail.com" | jq -r '.result[0].id // empty')
    [ "$s" = active ] && [ -n "$z" ] && { [ -z "$CF_ZONE_ID" ] || [ "$z" = "$CF_ZONE_ID" ]; } && echo "active, zone visible${CF_ZONE_ID:+ and equal to CF_ZONE_ID}" || { echo "status=$s zone_visible=$([ -n "$z" ] && echo yes || echo no)"; return 1; }
  }
  agent_token() { local s; s=$(curl -s -m 20 -K <(bearer "$CF_API_TOKEN_AGENT") "$CF_API/user/tokens/verify" | jq -r '.result.status // "error"'); [ "$s" = active ] && [ -n "$CF_ZONE_ID" ] && [ -n "$CF_ZONE_ID_TBPRO" ] && [ -n "$CF_ZONE_ID_DEVTM" ] && echo "active, three zone ids set" || { echo "status=$s"; return 1; }; }
  spf_final() { case "$SPF_FINAL_TXT" in "v=spf1 "*) echo "v=spf1 record, len=${#SPF_FINAL_TXT}" ;; *) echo "SPF_FINAL_TXT empty or not v=spf1 (len=${#SPF_FINAL_TXT})"; return 1 ;; esac; }
  pr_pair() {
    local repo=thunderbird/$1 pr=$2 rev=$3 head base
    [ -n "$pr" ] && [ -n "$rev" ] || { echo "PR numbers empty"; return 1; }
    head=$(gh pr view "$pr" -R "$repo" --json state,headRefName -q 'select(.state == "OPEN") | .headRefName')
    base=$(gh pr view "$rev" -R "$repo" --json state,baseRefName -q 'select(.state == "OPEN") | .baseRefName')
    [ -n "$head" ] && [ "$base" = "$head" ] && echo "#$pr open, revert #$rev open on its head" || { echo "#$pr head=${head:-not open}, revert #$rev base=${base:-not open}"; return 1; }
  }
  nlb_live() {
    local h st
    h=$(kstage -n thundermail get svc nginx -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)
    st=$([ -n "$h" ] && aws_stage elbv2 describe-load-balancers --query "LoadBalancers[?DNSName=='$h'].State.Code | [0]" --output text 2>/dev/null)
    [ -n "$h" ] && [ "$h" = "${TARGET_NLB_DNS:-}" ] && [ "$st" = active ] && echo "TARGET_NLB_DNS matches the live nginx Service, NLB active" || { echo "env=${TARGET_NLB_DNS:-empty} live=${h:-none} state=${st:-none}: rebuild the env file"; return 1; }
  }
  count_eq() { local got=$1 want=$2 what=$3; [ -n "$got" ] && [ -n "$want" ] && [ "$want" != 0 ] && [ "$got" = "$want" ] && echo "$what=$got" || { echo "$what=$got expected $want"; return 1; }; }
  pair() {
    local out snap
    out=$("$MIG_DIR/snapshot-$1.sh" ${3:-} 2>&1) || { echo "snapshot-$1.sh failed: $(tail -1 <<<"$out")"; return 1; }
    snap=$(tail -1 <<<"$out")
    [ -f "$snap" ] || { echo "snapshot-$1.sh wrote no file"; return 1; }
    "$MIG_DIR/restore-$2.sh" "$snap" ${4:-} --dry-run | tail -1
  }
  target_db() {
    if pod sh -c 'test -s /work/target-before.dump' 2>/dev/null; then "$MIG_DIR/restore-target-db.sh" /work/target-before.dump --dry-run | tail -1
    else "$MIG_DIR/restore-target-db.sh" --assert-only | tail -1; fi
  }

  chk secrets-non-empty nonempty
  chk operator-values operator_values
  chk dirs-and-kubeconfigs dirs
  chk runner-pod runner
  for h in STALWART_LEGACY_PG_HOST:STALWART_LEGACY_PG_USER:STALWART_LEGACY_PG_DB STALWART_TARGET_PG_HOST:STALWART_TARGET_PG_USER:STALWART_TARGET_PG_DB KC_LEGACY_PG_HOST:KC_LEGACY_PG_USER:KC_LEGACY_PG_DB ACCOUNTS_LEGACY_PG_HOST:ACCOUNTS_LEGACY_PG_USER:ACCOUNTS_LEGACY_PG_NAME; do
    IFS=: read -r hv uv dv <<<"$h"
    chk "pg_isready-${hv%_PG_HOST}" pod pg_isready -h "${!hv}"
    chk "psql-select1-${hv%_PG_HOST}" pod psql -h "${!hv}" -U "${!uv}" -d "${!dv}" -Atc 'select 1'
  done
  chk legacy-url-loopback legacy_loopback
  chk stalwart-legacy-healthz http200 "$OLD_STALWART_URL/healthz/live"
  chk stalwart-target-healthz http200 "$STALWART_URL/healthz/live"
  chk rclone-lsd-legacy bash -c '[ -n "$LEGACY_BLOB_BUCKET" ] && rclone lsd leg: | awk -v b="$LEGACY_BLOB_BUCKET" '\''$NF == b {f = 1} END {exit !f}'\'' && echo "bucket listed"'
  chk rclone-lsd-target bash -c '[ -n "$TARGET_BLOB_BUCKET" ] && rclone lsd tgt: | awk -v b="$TARGET_BLOB_BUCKET" '\''$NF == b {f = 1} END {exit !f}'\'' && echo "bucket listed"'
  chk legacy-sg count_eq "$(aws_legacy ec2 describe-security-groups --group-ids "$LEGACY_PUB_SG" --query 'length(SecurityGroups)' 2>/dev/null)" 1 security_groups
  chk legacy-sg-rule-ids count_eq "$(aws_legacy ec2 describe-security-group-rules --security-group-rule-ids $LEGACY_PUB_SG_RULE_IDS --query 'length(SecurityGroupRules)' 2>/dev/null)" "$(wc -w <<<"$LEGACY_PUB_SG_RULE_IDS")" rules
  chk legacy-ecs-services count_eq "$(for t in $LEGACY_ECS_TARGETS; do aws_legacy ecs describe-services --cluster "${t%%/*}" --services "${t##*/}" --query 'services[?status==`ACTIVE`].serviceName' --output text; done 2>/dev/null | grep -c .)" "$(wc -w <<<"$LEGACY_ECS_TARGETS")" active_services
  chk legacy-asg-targets count_eq "$(aws_legacy application-autoscaling describe-scalable-targets --service-namespace ecs --resource-ids $LEGACY_ASG_RESOURCE_IDS --query 'length(ScalableTargets)' 2>/dev/null)" "$(wc -w <<<"$LEGACY_ASG_RESOURCE_IDS")" scalable_targets
  chk legacy-mgmt-instance count_eq "$(aws_legacy ec2 describe-instances --instance-ids "$LEGACY_MGMT_INSTANCE" --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)" running state
  chk legacy-pub-lb count_eq "$(aws_legacy elbv2 describe-load-balancers --load-balancer-arns "$LEGACY_PUB_LB_ARN" --query 'length(LoadBalancers)' 2>/dev/null)" 1 load_balancers
  chk target-nlb nlb_live
  chk argo-apps count_eq "$(khub -n argocd get application $ARGO_APPS -o name 2>/dev/null | grep -c .)" "$(wc -w <<<"$ARGO_APPS")" applications
  chk kargo-stage-accounts khub -n "$KARGO_PROJECT_ACCOUNTS" get stage "$KARGO_STAGE_ACCOUNTS" -o name
  chk kargo-stage-configs khub -n "$KARGO_PROJECT_CONFIGS" get stage "$KARGO_STAGE_CONFIGS" -o name
  chk kargo-warehouse-configs khub -n "$KARGO_PROJECT_CONFIGS" get warehouse "$KARGO_WAREHOUSE_CONFIGS" -o name
  chk cf-token-shared-scope shared_scope
  chk cf-token-agent agent_token
  chk spf-final-txt spf_final
  chk t7-prs pr_pair keycloak-customer-deploy "${T7_PR:-}" "${T7_REVERT_PR:-}"
  chk t8-prs pr_pair accounts-deploy "${T8_PR:-}" "${T8_REVERT_PR:-}"
  chk restore-legacy-sg-dry-run pair legacy-sg legacy-sg
  chk restore-ecs-dry-run pair ecs ecs
  chk restore-replicas-dry-run bash -o pipefail -c '"$0"/snapshot-gitops.sh >/dev/null && "$0"/restore-replicas.sh "$(ls -1t "$EVIDENCE_DIR"/replicas-before-2*.txt | head -1)" --dry-run | tail -1' "$MIG_DIR"
  chk restore-zone-dry-run pair zone zone "$CF_ZONE_ID"
  chk restore-realm-dry-run pair realm realm "$KC_LEGACY_URL" "$KC_LEGACY_URL"
  chk restore-secret-version-dry-run bash -o pipefail -c '"$0"/restore-secret-version.sh --check $1 >/dev/null && "$0"/snapshot-secret-versions.sh $1 >/dev/null && n=0 && while read -r id v; do "$0"/restore-secret-version.sh "$id" "$v" --dry-run >/dev/null && n=$((n + 1)) || exit 1; done < "$(ls -1t "$EVIDENCE_DIR"/secret-versions-before-*.txt | head -1)" && echo "secrets=$n checked, list and dry-run ok"' "$MIG_DIR" "$secret_ids"
  chk restore-target-db-dry-run target_db
  local end; end=$(date -u +%FT%TZ)
  printf 'env-check\t%s\t%s\tfailures\t%s\n' "$start" "$end" "$fails" >> "$TIMING_TSV"
  printf 'env-check start=%s end=%s failures=%s (row appended to %s)\n' "$start" "$end" "$fails" "$TIMING_TSV"
  [ "$fails" = 0 ]
}

case "$MODE" in
  build) build ;;
  check) check ;;
esac
