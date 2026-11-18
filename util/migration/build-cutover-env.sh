#!/usr/bin/env bash
set -euo pipefail
MODE=build
REBUILD=0
for a in "$@"; do
  case "$a" in
    --check) MODE=check ;;
    --rebuild) REBUILD=1 ;;
    -h|--help) echo 'usage: TARGET_ENV=<env> build-cutover-env.sh [--check | --rebuild]'; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ENV_FILE=${CUTOVER_ENV:-/dev/shm/talos/cutover-$TARGET_ENV.env}
OPERATOR_ENV=${OPERATOR_ENV:-$HOME/.local/state/talos/cutover-operator.env}
load_operator_env "$OPERATOR_ENV"
VALUES=$REPO_DIR/$CONFIGS_OVERLAY/config/values.yaml
NEON_API=https://console.neon.tech/api/v2
CARRY="FREEZE_SHA THAW_PR T7_ENV_SHA T8_ENV_SHA T6_SHA FREIGHT STALWART_ADMIN_ACCOUNT_ID CURRENT_ACCOUNTS_FREIGHT THAW_SHA T7_START T8_START DECISIONS_COMMENT_URL CUT_ECS_END TEST_SENDER_APP_PASSWORD PADDLE_NOTIFICATION_ID NOTICE_RECIPIENTS_CSV"
OPERATOR_VARS="FREEZE_PR T6_PR FLIP_PR APPOINTMENT_PR ADMIN_MERGE SPF_FINAL_TXT TEST_MAILBOX TEST_MAILBOX_IMAP_HOST TEST_MAILBOX_IMAP_USER TEST_MAILBOX_IMAP_PASSWORD TEST_USER SUPPORT_CHANNEL INBOUND_FROM"
WINDOW_CARRY="REPLICAS_BEFORE STATE_INBOUND_AT STATE_OPEN_AT AUTH_DNS_AT ACCOUNTS_DNS_AT INBOUND_TTL_BEFORE INBOUND_TTL_WAIT KC_IMAGE_VERSION INBOUND_FROM ACCOUNTS_AUTOSYNC_SHA SECRET_VERSIONS_BEFORE WINDOW_SHA WINDOW_END REALM_BEFORE LEGACY_EGRESS_SNAPSHOT BACKUP_SECOND_COPY"
WINDOW_OPERATOR_VARS="PROBE_TIMEOUT ACCOUNTS_AUTOSYNC_PR REC_AUTH REC_ACCOUNTS MAIL_RECORDS_TSV TEST_USER_TBPRO PILOT_USER PROBE_MX_HOST PROBE_DOMAIN PROBE_MAILBOX PROBE_SENDER PROBE_HELO TEST_MAILBOX_GMAIL TEST_MAILBOX_M365 JUNK_BAND WINDOW_END ROLLBACK_HORIZON GATE_MARGIN CERT_MIN_NOT_AFTER OUTBOUND_PROOF_INTERVAL OUTBOUND_PROOF_ATTEMPTS VERIFY_BODY_SAMPLE QUOTA_RESET_TIMEOUT W4_DUMP_MAX_AGE REHEARSAL_SHA WATCH_INTERVAL LANE_PROBE_MAX LANE_REPORT_MAX LANE_WAIT_MAX PADDLE_POLL_MAX MAIL_BACKUP_STORE ROTATION_HOST PROVISIONING_RELEASE_PR G_IN_DEADLINE"
SECRET_VARS="STALWART_TARGET_PG_PASSWORD STALWART_PASSWORD OLD_STALWART_PASSWORD STALWART_LEGACY_PG_PASSWORD ACCOUNTS_LEGACY_PG_PASSWORD KC_LEGACY_PG_PASSWORD KC_LEGACY_ADMIN_CLIENT_SECRET CF_API_TOKEN_AGENT PADDLE_API_KEY TEST_MAILBOX_IMAP_PASSWORD"

try() { ( "$@" ) 2>/dev/null || true; }
prev() { [ -f "$ENV_FILE" ] || return 0; ( set +eu; . "$ENV_FILE" >/dev/null 2>&1; printf '%s' "${!1:-}" ); }
op() { printf '%s' "${!1:-$(prev "$1")}"; }
from_file() { [ -f "$1" ] && sed -n "s/^\(export \)\{0,1\}$2=//p" "$1" | head -1 | sed "s/^['\"]//; s/['\"]\$//" || true; }
bearer() { printf 'header = "Authorization: Bearer %s"\n' "$1"; }
sm_legacy() { aws_legacy secretsmanager get-secret-value --secret-id "$1" --output json | jq -r '.SecretString'; }
sm_target() { aws_target secretsmanager get-secret-value --secret-id "$1" --output json | jq -r '.SecretString'; }
k8s_secret() { ktarget -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }
direct() { sed 's/-pooler//'; }
doh_a() { curl -s -m 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=A" | jq -r '[.Answer[]? | select(.type == 1) | .data] | sort | join(" ")'; }
opw() { local v; v=$(op "$1"); [ -n "$v" ] || [ "${REBUILD:-0}" != 1 ] || v=$(window_values "$1"); printf '%s' "$v"; }
window_values() { [ -f "$EVIDENCE_DIR/window-values.tsv" ] && awk -F'\t' -v n="$1" '$1 == n {v = $2; f = 1} END {if (f) printf "%s", v}' "$EVIDENCE_DIR/window-values.tsv" || true; }
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

render_ref() {
  local d out
  d=$(mktemp -d) || return 1
  out=$(git -C "$1" archive "$2" | tar -x -C "$d" && kubectl kustomize "$d/$3" | yq -r "select(.kind == \"$4\" and .metadata.name == \"$5\") | ${6:-.spec.replicas}" | grep -v '^null$' | head -1)
  rm -rf "$d"
  printf '%s' "$out"
}
held_replicas() {
  local dir=$1 pr=$2 kind=$3 name=$4 want=$5 got
  [ -n "$pr" ] && [ -d "$dir/.git" ] && [ -n "$want" ] || { echo "PR, checkout or env file count missing"; return 1; }
  git -C "$dir" fetch -q origin "pull/$pr/head" || { echo "git fetch of pull/$pr/head failed in $dir"; return 1; }
  got=$(render_ref "$dir" FETCH_HEAD "$IDENTITY_OVERLAY" "$kind" "$name")
  [ "$got" = "$want" ] && echo "#$pr head renders $name replicas=$got" || { echo "#$pr head renders $name replicas=${got:-none}, env file has $want"; return 1; }
}
window_vars() {
  local app m alb ids vpc kcd=${KCD_DIR:-$HOME/src/tb/keycloak-customer-deploy} acd=${ACD_DIR:-$HOME/src/tb/accounts-deploy} v
  put TARGET_NLB_IPS "$([ -n "$nlb" ] && try doh_a "$nlb")" "DoH A records of TARGET_NLB_DNS at 1.1.1.1 (every address of the mail load balancer)"
  for app in keycloak-customer:AUTH accounts:ACCOUNTS; do
    alb=$(try ktarget -n "${app%%:*}" get ingress -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}')
    put "TBPROD_${app##*:}_ALB" "$alb" "kubectl -n ${app%%:*} get ingress .items[0].status.loadBalancer.ingress[0].hostname"
    put "TBPROD_${app##*:}_ALB_IP" "$([ -n "$alb" ] && try doh_a "$alb")" "DoH A records of TBPROD_${app##*:}_ALB at 1.1.1.1"
  done
  put KEYCLOAK_REPLICAS "$(try render_ref "$kcd" "origin/$IDENTITY_ENV_BRANCH" "$IDENTITY_OVERLAY" StatefulSet keycloak-customer)" "kubectl kustomize of $IDENTITY_OVERLAY at origin/$IDENTITY_ENV_BRANCH in KCD_DIR, StatefulSet keycloak-customer .spec.replicas"
  put ACCOUNTS_WEB_REPLICAS "$(try render_ref "$acd" "origin/$IDENTITY_ENV_BRANCH" "$IDENTITY_OVERLAY" Deployment accounts-web)" "kubectl kustomize of $IDENTITY_OVERLAY at origin/$IDENTITY_ENV_BRANCH in ACD_DIR, Deployment accounts-web .spec.replicas"
  put ACCOUNTS_CELERY_REPLICAS "$(try render_ref "$acd" "origin/$IDENTITY_ENV_BRANCH" "$IDENTITY_OVERLAY" Deployment accounts-celery)" "kubectl kustomize of $IDENTITY_OVERLAY at origin/$IDENTITY_ENV_BRANCH in ACD_DIR, Deployment accounts-celery .spec.replicas"
  put STALWART_REPLICAS "$(try render_ref "$REPO_DIR" HEAD "$TARGET_OVERLAY" HorizontalPodAutoscaler stalwart .spec.minReplicas)" "kubectl kustomize $TARGET_OVERLAY at HEAD, HorizontalPodAutoscaler stalwart .spec.minReplicas"
  put TARGET_STALWART_SG "$(try render_ref "$REPO_DIR" HEAD "$TARGET_OVERLAY" SecurityGroupPolicy stalwart-server '.spec.securityGroups.groupIds[0]')" "kubectl kustomize $TARGET_OVERLAY at HEAD, SecurityGroupPolicy stalwart-server groupIds[0]"
  vpc=$(try aws_target eks describe-cluster --name "$TARGET_CLUSTER" --query cluster.resourcesVpcConfig.vpcId --output text)
  put TARGET_STALWART_NAT_IDS "$([ -n "$vpc" ] && try aws_target ec2 describe-nat-gateways --filter "Name=vpc-id,Values=$vpc" Name=state,Values=available --query 'NatGateways[].NatGatewayId' --output text | tr '\t' '\n' | sort | tr '\n' ' ' | sed 's/ $//')" "aws ec2 describe-nat-gateways --profile $TARGET_AWS_PROFILE in the VPC of $TARGET_CLUSTER, available"
  for app in $ARGO_APP_OF_APPS $ARGO_APP_THUNDERMAIL $ARGO_APP_ACCOUNTS $ARGO_APP_KEYCLOAK; do
    m=$(try khub -n argocd get application "$app" -o json | jq -r 'if .spec.syncPolicy.automated then "automated" else "manual" end')
    put "ARGO_SYNC_MODE_$(tr 'a-z-' 'A-Z_' <<<"$app")" "$m" "kubectl -n argocd get application $app on the hub: .spec.syncPolicy.automated set means automated"
  done
  r=$(neon_lookup STALWART_TARGET "$(jq -r '.host // empty' <<<"${cfg:-{\}}" | direct)")
  put NEON_PROJECT_STALWART_TARGET "$(op NEON_PROJECT_STALWART_TARGET)$([ -n "$(op NEON_PROJECT_STALWART_TARGET)" ] || cut -f1 <<<"$r")" "NEON_PROJECT_STALWART_TARGET in the environment, else Neon API: project whose endpoint id is the STALWART_TARGET_PG_HOST prefix"
  v=$(op NEON_BRANCH_KC); put NEON_BRANCH_KC "${v:-main}" "NEON_BRANCH_KC in the environment, else main"
  v=$(op NEON_BRANCH_ACCOUNTS); put NEON_BRANCH_ACCOUNTS "${v:-main}" "NEON_BRANCH_ACCOUNTS in the environment, else main"
  ids=$(try aws_legacy ec2 describe-instances --filters "Name=tag:Name,Values=$LEGACY_NODE_NAME_FILTER" Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' '\n' | grep . | sort | tr '\n' ' ' | sed 's/ $//')
  [ "$(wc -w <<<"$ids")" = "$LEGACY_NODE_COUNT" ] || ids=""
  put LEGACY_STALWART_NODE_IDS "$ids" "aws ec2 describe-instances --profile $LEGACY_AWS_PROFILE Name=$LEGACY_NODE_NAME_FILTER running; empty unless exactly $LEGACY_NODE_COUNT"
  put LEGACY_PG_SECRET_NAME "$LEGACY_PG_SECRET_NAME" "targets/$TARGET_ENV.sh"
  v=$(opw INBOUND_TTL_WAIT); put INBOUND_TTL_WAIT "${v:-60}" "INBOUND_TTL_WAIT appended in the window, else 60 (the inbound record TTL of #1726)"
  v=$(opw WINDOW_SHA); put WINDOW_SHA "${v:-$(op REHEARSAL_SHA)}" "WINDOW_SHA appended by a go-fix merge, else REHEARSAL_SHA"
  put TMD_CHECKOUT "$(op TMD_CHECKOUT)" "operator: the clean thundermail-deploy clone the window runs from"
  put CUTOVER_ENV "$ENV_FILE" "this file"
  for v in $WINDOW_OPERATOR_VARS $(compgen -v W4_WAIT_ || true); do
    case "$PUT" in *" $v "*) continue ;; esac
    put "$v" "$(opw "$v")" "operator: $v (environment or $OPERATOR_ENV)"
  done
}

build() {
  KUBECONFIG_TARGET=$(realpath -m "${KUBECONFIG_TARGET:-$HOME/.local/state/talos/$TARGET_KUBECONFIG_NAME}")
  KUBECONFIG_HUB=$(realpath -m "${KUBECONFIG_HUB:-$HOME/.local/state/talos/kc-hub}")
  KUBECONFIG_DEV=$(realpath -m "${KUBECONFIG_DEV:-$HOME/.local/state/talos/kc-dev}")
  mkdir -p "$(dirname "$ENV_FILE")" "$EVIDENCE_DIR" "$SECRET_DIR"
  env_path_ok "$ENV_FILE" || die "refusing $ENV_FILE: it or its directory is a symlink, not owned by $(id -un), or writable by group or others"
  local count=0 missing=0
  PUT=" "
  [ ! -f "$ENV_FILE" ] || env_target_ok "$ENV_FILE" || exit 64
  if [ "$TARGET_ENV" = tb-prod ] && [ -n "$(op FREEZE_SHA)" ] && [ "$REBUILD" != 1 ]; then die "FREEZE_SHA is set: the window is open, rerun with --rebuild"; fi
  tmp=$(mktemp "$(dirname "$ENV_FILE")/.cutover-$TARGET_ENV.XXXXXX")
  trap 'rm -f "$tmp"' EXIT
  put() {
    local name=$1 value=$2 source=$3 q
    q=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
    printf '# source: %s\n%s=%s\n' "$source" "$name" "'$q'" >> "$tmp"
    count=$((count + 1))
    PUT="$PUT$name "
    if [[ $value == *REPLACE_ME_* ]]; then printf '%-30s len=%s NEED (placeholder)\n' "$name" "${#value}"; missing=$((missing + 1))
    elif [ "$TARGET_ENV" = tb-prod ] && [[ ${value,,} == *stage* ]]; then printf '%-30s len=%s CONTAINS stage\n' "$name" "${#value}"; missing=$((missing + 1))
    elif [ -n "$value" ]; then printf '%-30s len=%s\n' "$name" "${#value}"; else printf '%-30s len=0 NEED\n' "$name"; missing=$((missing + 1)); fi
  }
  printf '# built %s by util/migration/build-cutover-env.sh; mode 600; never commit\n' "$(date -u +%FT%TZ)" > "$tmp"
  printf "# source: the TARGET_ENV of this build\nTARGET_ENV='%s'\n" "$TARGET_ENV" >> "$tmp"

  local cfg; cfg=$(try ktarget -n thundermail get cm stalwart-config -o jsonpath='{.data.config\.json}')
  put STALWART_TARGET_PG_HOST "$(jq -r '.host // empty' <<<"${cfg:-{\}}" | direct)" "kubectl -n thundermail get cm stalwart-config (config.json host, -pooler stripped)"
  put STALWART_TARGET_PG_DB "$(jq -r '.database // empty' <<<"${cfg:-{\}}")" "kubectl -n thundermail get cm stalwart-config (config.json database)"
  put STALWART_TARGET_PG_USER "$(jq -r '.authUsername // empty' <<<"${cfg:-{\}}")" "kubectl -n thundermail get cm stalwart-config (config.json authUsername)"
  put STALWART_TARGET_PG_PASSWORD "$(try k8s_secret thundermail stalwart-postgresql-admin-password STALWART_POSTGRESQL_ADMIN_PASSWORD)" "kubectl -n thundermail get secret stalwart-postgresql-admin-password key STALWART_POSTGRESQL_ADMIN_PASSWORD"
  put STALWART_USER "$(try k8s_secret thundermail stalwart-recovery-admin STALWART_USER)" "kubectl -n thundermail get secret stalwart-recovery-admin key STALWART_USER"
  put STALWART_PASSWORD "$(try k8s_secret thundermail stalwart-recovery-admin STALWART_PASSWORD)" "kubectl -n thundermail get secret stalwart-recovery-admin key STALWART_PASSWORD"
  [ "$TARGET_ENV" = tb-prod ] || put STALWART_URL http://localhost:18081 "port-forward to the Stalwart pod, 18081:8080"
  put OLD_STALWART_URL https://127.0.0.1:18080 "SSM port-forward to LEGACY_MGMT_INSTANCE 8080; management listener is implicit TLS"
  put OLD_STALWART_USER admin "legacy fallback admin"
  put OLD_STALWART_PASSWORD "$(try sm_legacy "$LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.fallback_admin_password")" "aws secretsmanager get-secret-value --profile $LEGACY_AWS_PROFILE $LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.fallback_admin_password"

  local lpg; lpg=$(try sm_legacy "$LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend")
  put STALWART_LEGACY_PG_HOST "$(jq -r '.host // empty' <<<"${lpg:-{\}}" | direct)" "SM $LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend .host (-pooler stripped)"
  put STALWART_LEGACY_PG_DB "$(jq -r '.database // empty' <<<"${lpg:-{\}}")" "SM $LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend .database"
  put STALWART_LEGACY_PG_USER "$(jq -r '.user // empty' <<<"${lpg:-{\}}")" "SM $LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend .user"
  put STALWART_LEGACY_PG_PASSWORD "$(jq -r '.password // empty' <<<"${lpg:-{\}}")" "SM $LEGACY_STALWART_SECRET_PREFIX/stalwart.postboot.postgresql_backend .password"
  put ACCOUNTS_LEGACY_PG_HOST "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/database-host" | direct)" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/database-host (-pooler stripped)"
  put ACCOUNTS_LEGACY_PG_NAME "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/database-name")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/database-name"
  put ACCOUNTS_LEGACY_PG_USER "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/database-user")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/database-user"
  put ACCOUNTS_LEGACY_PG_PASSWORD "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/database-password")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/database-password"
  put KC_LEGACY_PG_HOST "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-host" | direct)" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-host (-pooler stripped)"
  put KC_LEGACY_PG_DB "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-name")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-name"
  put KC_LEGACY_PG_USER "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-user")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-user"
  put KC_LEGACY_PG_PASSWORD "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-password")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-password"
  put KC_LEGACY_ADMIN_CLIENT_ID "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-admin-client-id")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-admin-client-id"
  put KC_LEGACY_ADMIN_CLIENT_SECRET "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-admin-client-secret")" "SM $LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-admin-client-secret"
  put KC_LEGACY_URL "https://$LEGACY_AUTH_HOST" "legacy Keycloak of $TARGET_ENV"

  NEON_KEY=$(from_file "$HOME/.neon-api.env" NEON_API_KEY)
  local org; org=$(from_file "$HOME/.neon-api.env" NEON_ORG_ID)
  NEON_PROJECTS=""
  declare -A NEON_EP_CACHE=()
  [ -n "$NEON_KEY" ] && NEON_PROJECTS=$(try neon_get "projects?limit=400${org:+&org_id=$org}" | jq -r '.projects[]?.id' | tr '\n' ' ')
  local r
  r=$(neon_lookup STALWART_LEGACY "$(jq -r '.host // empty' <<<"${lpg:-{\}}" | direct)")
  put NEON_PROJECT_STALWART_LEGACY "$(op NEON_PROJECT_STALWART_LEGACY)$([ -n "$(op NEON_PROJECT_STALWART_LEGACY)" ] || cut -f1 <<<"$r")" "NEON_PROJECT_STALWART_LEGACY in the environment, else Neon API: project whose endpoint id is the STALWART_LEGACY_PG_HOST prefix"
  put NEON_LEGACY_STALWART_BRANCH "$(cut -f2 <<<"$r")" "Neon API: branch of that endpoint"
  r=$(neon_lookup KC "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/keycloak-database-host" | direct)")
  put NEON_PROJECT_KC "$(op NEON_PROJECT_KC)$([ -n "$(op NEON_PROJECT_KC)" ] || cut -f1 <<<"$r")" "NEON_PROJECT_KC in the environment, else Neon API: project whose endpoint id is the KC_LEGACY_PG_HOST prefix"
  r=$(neon_lookup ACCOUNTS "$(try sm_legacy "$LEGACY_ACCOUNTS_SECRET_PREFIX/database-host" | direct)")
  put NEON_PROJECT_ACCOUNTS "$(op NEON_PROJECT_ACCOUNTS)$([ -n "$(op NEON_PROJECT_ACCOUNTS)" ] || cut -f1 <<<"$r")" "NEON_PROJECT_ACCOUNTS in the environment, else Neon API: project whose endpoint id is the ACCOUNTS_LEGACY_PG_HOST prefix"

  put LEGACY_BLOB_BUCKET "$(try aws_legacy s3api list-buckets --query "Buckets[?contains(Name,'$LEGACY_BUCKET_MATCH') && ends_with(Name,'-stalwart-s3-store')].Name | [0]" --output text | grep -v '^None$' || true)" "aws s3api list-buckets --profile $LEGACY_AWS_PROFILE (name *$LEGACY_BUCKET_MATCH*-stalwart-s3-store)"
  put TARGET_BLOB_BUCKET "$(yq -r '.blobStoreBucket // ""' "$VALUES")" "$CONFIGS_OVERLAY/config/values.yaml blobStoreBucket"
  put REDIS_URL "$(yq -r '.inMemoryStoreURL // ""' "$VALUES")" "$CONFIGS_OVERLAY/config/values.yaml inMemoryStoreURL"

  put LEGACY_MGMT_INSTANCE "$(try aws_legacy ec2 describe-instances --filters "Name=tag:Name,Values=$LEGACY_MGMT_NAME_FILTER" Name=instance-state-name,Values=running --query 'Reservations[0].Instances[0].InstanceId' --output text | grep -v '^None$' || true)" "aws ec2 describe-instances --profile $LEGACY_AWS_PROFILE Name=$LEGACY_MGMT_NAME_FILTER running"
  local lb; lb=$(try aws_legacy elbv2 describe-load-balancers --names "$LEGACY_PUB_LB_NAME" --query 'LoadBalancers[0].[LoadBalancerArn,SecurityGroups[0]]' --output text)
  put LEGACY_PUB_LB_ARN "$(cut -f1 <<<"$lb")" "aws elbv2 describe-load-balancers --profile $LEGACY_AWS_PROFILE --names $LEGACY_PUB_LB_NAME"
  local sg; sg=$(cut -f2 <<<"$lb")
  put LEGACY_PUB_SG "$sg" "aws elbv2 describe-load-balancers --profile $LEGACY_AWS_PROFILE --names $LEGACY_PUB_LB_NAME SecurityGroups[0]"
  put LEGACY_PUB_SG_RULE_IDS "$([ -n "$sg" ] && try aws_legacy ec2 describe-security-group-rules --filters "Name=group-id,Values=$sg" --query 'SecurityGroupRules[?IsEgress==`false`].SecurityGroupRuleId' --output text | tr '\t' '\n' | sort | tr '\n' ' ' | sed 's/ $//')" "aws ec2 describe-security-group-rules --profile mzla-legacy group-id=LEGACY_PUB_SG, ingress"
  local clusters targets=""
  clusters=$(try aws_legacy ecs list-clusters --query 'clusterArns[]' --output text | tr '\t' '\n' | sed 's|.*/||' | { grep "^$LEGACY_ECS_CLUSTER_PREFIX" || true; } | sort)
  for c in $clusters; do targets="$targets $(try aws_legacy ecs list-services --cluster "$c" --query 'serviceArns[]' --output text | tr '\t' '\n' | sed "s|.*/||; s|^|$c/|" | sort | tr '\n' ' ')"; done
  targets=$(echo $targets)
  put LEGACY_ECS_TARGETS "$targets" "aws ecs list-clusters/list-services --profile $LEGACY_AWS_PROFILE, clusters $LEGACY_ECS_CLUSTER_PREFIX*"
  local rids=""
  for t in $targets; do rids="$rids service/$t"; done
  put LEGACY_ASG_RESOURCE_IDS "$([ -n "$rids" ] && try aws_legacy application-autoscaling describe-scalable-targets --service-namespace ecs --resource-ids $rids --query 'ScalableTargets[].ResourceId' --output text | tr '\t' ' ')" "aws application-autoscaling describe-scalable-targets --profile mzla-legacy for LEGACY_ECS_TARGETS"

  put ARGO_APPS "$ARGO_APP_OF_APPS $ARGO_APP_THUNDERMAIL $ARGO_APP_ACCOUNTS $ARGO_APP_KEYCLOAK" "platform-infrastructure argocd/ app names (targets/$TARGET_ENV.sh)"
  put KARGO_PROJECT_ACCOUNTS "$KARGO_PROJECT_ACCOUNTS" "Kargo project (targets/$TARGET_ENV.sh)"
  put KARGO_STAGE_ACCOUNTS "$KARGO_STAGE_ACCOUNTS" "Kargo stage (targets/$TARGET_ENV.sh)"
  put KARGO_PROJECT_CONFIGS "$KARGO_PROJECT_CONFIGS" "Kargo project (targets/$TARGET_ENV.sh)"
  put KARGO_STAGE_CONFIGS "$KARGO_STAGE_CONFIGS" "Kargo stage (targets/$TARGET_ENV.sh)"
  put KARGO_WAREHOUSE_CONFIGS "$KARGO_WAREHOUSE_CONFIGS" "Kargo warehouse (targets/$TARGET_ENV.sh)"
  put FREEZE_PR "$(op FREEZE_PR)$([ -z "$(op FREEZE_PR)" ] && try gh pr list -R thunderbird/platform-infrastructure --head "$FREEZE_BRANCH" --state open --json number --jq '.[0].number')" "gh pr list -R thunderbird/platform-infrastructure --head $FREEZE_BRANCH, or FREEZE_PR in the environment"
  put T6_PR "$(op T6_PR)" "operator: TMD values.yaml PR number (T6_PR in the environment)"
  for d in "T7_PR:keycloak-customer-deploy:$PR_HEAD_PREFIX-t7" "T7_REVERT_PR:keycloak-customer-deploy:$PR_HEAD_PREFIX-t7-revert" "T8_PR:accounts-deploy:$PR_HEAD_PREFIX-t8" "T8_REVERT_PR:accounts-deploy:$PR_HEAD_PREFIX-t8-revert"; do
    local v repo head n
    IFS=: read -r v repo head <<<"$d"
    n=$(op "$v")
    [ -n "$n" ] || n=$(try pr_by_head "$repo" "$head")
    put "$v" "$n" "$v in the environment, else the one open thunderbird/$repo PR with head */$head (gh pr list)"
  done
  put FLIP_PR "$(op FLIP_PR)" "operator: TMD dnsManagement Automatic PR number (FLIP_PR in the environment)"
  put APPOINTMENT_PR "$(op APPOINTMENT_PR)" "operator: appointment $TARGET_ENV PR number (APPOINTMENT_PR in the environment)"
  put ADMIN_MERGE "$(op ADMIN_MERGE)" "operator: 1 = merge_pr admin-merges REVIEW_REQUIRED window PRs after green checks and Greptile 5/5 (decision 2026-10-28)"
  for d in PI_DIR:platform-infrastructure KCD_DIR:keycloak-customer-deploy ACD_DIR:accounts-deploy APPOINTMENT_DIR:appointment TMD_DIR:thundermail-deploy; do
    local v=${d%%:*} repo=${d##*:} path
    path=${!v:-$HOME/src/tb/$repo}
    [ -d "$path/.git" ] || path=""
    put "$v" "$path" "local checkout of thunderbird/$repo ($v in the environment, default \$HOME/src/tb/$repo)"
  done

  put AWS_REGION "$AWS_REGION" "targets/$TARGET_ENV.sh TARGET_REGION"
  CF_TOKEN=$(from_file "$HOME/.cloudflare.env" CF_API_TOKEN_AGENT)
  put CF_API_TOKEN_AGENT "$CF_TOKEN" "\$HOME/.cloudflare.env CF_API_TOKEN_AGENT"
  local zid; zid=$(try cf_zone "$MAIL_ZONE")
  [ -n "$zid" ] || zid=$(try shared_zone_id "$MAIL_ZONE")
  put CF_ZONE_ID "$zid" "GET /zones?name=$MAIL_ZONE with CF_API_TOKEN_AGENT, else with the shared external-dns token (read-only)"
  put CF_ZONE_ID_TBPRO "$(try cf_zone "$TBPRO_ZONE")" "GET /zones?name=$TBPRO_ZONE"
  put PADDLE_API_KEY "$(from_file "$HOME/.paddle.env" PADDLE_API_KEY)" "\$HOME/.paddle.env PADDLE_API_KEY"
  put ACCOUNTS_WEBHOOK_URL "https://$ACCOUNTS_HOST/api/v1/telemetry/stalwart/webhook/" "$TARGET_ENV Accounts telemetry webhook"
  local nlb; nlb=$(try ktarget -n thundermail get svc nginx -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
  [ -n "$nlb" ] || nlb=$(try aws_target elbv2 describe-load-balancers --names "$TARGET_NLB_NAME" --query 'LoadBalancers[0].DNSName' --output text | grep -v '^None$' || true)
  put TARGET_NLB_DNS "$nlb" "kubectl -n thundermail get svc nginx .status.loadBalancer.ingress[0].hostname, resolved live on every build (else aws elbv2 --names $TARGET_NLB_NAME)"
  put TEST_MAILBOX "$(op TEST_MAILBOX)" "operator: external test mailbox; must be TEST_USER's Keycloak email (the backup address)"
  put TEST_MAILBOX_IMAP_HOST "$(op TEST_MAILBOX_IMAP_HOST)" "operator: external test mailbox IMAP host"
  put TEST_MAILBOX_IMAP_USER "$(op TEST_MAILBOX_IMAP_USER)" "operator: external test mailbox IMAP user"
  put TEST_MAILBOX_IMAP_PASSWORD "$(op TEST_MAILBOX_IMAP_PASSWORD)" "operator: external test mailbox IMAP password"
  put TEST_USER "$(op TEST_USER)" "operator: migrated $TARGET_ENV mailbox the operator owns"
  put TEST_SENDER "$(op TEST_USER)" "same as TEST_USER"
  put INBOUND_FROM "$(op INBOUND_FROM)" "operator: sender of the W7 inbound test, in a domain whose SPF passes for the W7 host and not the domain of the zone, TEST_MAILBOX or TEST_USER; no default"
  put SPF_FINAL_TXT "$(op SPF_FINAL_TXT)" "operator: spf. TXT from the #1535 decision (SPF_FINAL_TXT in the environment)"
  put SUPPORT_CHANNEL "$(op SUPPORT_CHANNEL)" "operator: support announcement channel"
  for v in KUBECONFIG_TARGET KUBECONFIG_HUB KUBECONFIG_DEV; do
    put "$v" "${!v}" "aws eks update-kubeconfig --kubeconfig ${!v} ($v in the environment$([ "$v" != KUBECONFIG_TARGET ] || echo ", cluster $TARGET_CLUSTER, profile $TARGET_AWS_PROFILE"))"
    [ -f "${!v}" ] || { printf '%-30s file missing\n' "$v"; missing=$((missing + 1)); }
  done
  put EVIDENCE_DIR "$EVIDENCE_DIR" "evidence directory"
  local tsv; tsv=$(op TIMING_TSV); put TIMING_TSV "${tsv:-$EVIDENCE_DIR/timing-$TARGET_ENV.tsv}" "TIMING_TSV in the environment or the previous file, else \$EVIDENCE_DIR/timing-$TARGET_ENV.tsv"
  put SECRET_DIR "$SECRET_DIR" "tmpfs directory for secret artifacts"
  for side in "LEG:$LEGACY_AWS_PROFILE" "TGT:$TARGET_AWS_PROFILE"; do
    local p=${side%%:*}
    put "RCLONE_CONFIG_${p}_TYPE" s3 rclone
    put "RCLONE_CONFIG_${p}_PROVIDER" AWS rclone
    put "RCLONE_CONFIG_${p}_ENV_AUTH" true rclone
    put "RCLONE_CONFIG_${p}_REGION" "$AWS_REGION" rclone
    put "RCLONE_CONFIG_${p}_PROFILE" "${side##*:}" rclone
  done
  [ "$TARGET_ENV" != tb-prod ] || window_vars
  [ "$TARGET_ENV" != tb-prod ] || CARRY="$CARRY $WINDOW_CARRY"
  for name in $CARRY; do
    case "${PUT:- }" in *" $name "*) continue ;; esac
    local v; v=$(op "$name")
    [ "$name" != TEST_SENDER_APP_PASSWORD ] || v=$(prev "$name")
    if [ -z "$v" ] && [ "${REBUILD:-0}" = 1 ]; then
      case " $SECRET_VARS TEST_SENDER_APP_PASSWORD " in *" $name "*) ;; *) v=$(window_values "$name") ;; esac
      [ -n "$v" ] || printf '%-30s not restored: no copy in %s\n' "$name" "$EVIDENCE_DIR/window-values.tsv"
    fi
    [ -n "$v" ] || continue
    printf '# carried over from the previous env file\n%s=%s\n' "$name" "'$(printf '%s' "$v" | sed "s/'/'\\\\''/g")'" >> "$tmp"
    printf '%-30s len=%s (carried)\n' "$name" "${#v}"
  done
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
  printf 'written %s (mode %s) vars=%s missing=%s\n' "$ENV_FILE" "$(stat -c %a "$ENV_FILE")" "$count" "$missing"
  old=$HOME/.local/state/talos/cutover-$TARGET_ENV.env
  [ "$old" = "$ENV_FILE" ] || [ ! -e "$old" ] || { printf 'old env file %s is on persistent disk: delete it\n' "$old"; missing=$((missing + 1)); }
  [ "$missing" = 0 ]
}

check() {
  env_path_ok "$ENV_FILE" || die "refusing $ENV_FILE: it or its directory is a symlink, not owned by $(id -un), or writable by group or others"
  [ -f "$ENV_FILE" ] || die "$ENV_FILE missing: run build-cutover-env.sh first"
  env_target_ok "$ENV_FILE" || exit 64
  local want=$TARGET_ENV
  set -a; . "$ENV_FILE"; set +a
  TARGET_ENV=$want
  if [ -n "${LANE_PF_PORT:-}" ]; then STALWART_URL=http://localhost:$LANE_PF_PORT; elif [ "$TARGET_ENV" = tb-prod ]; then STALWART_URL=""; fi
  . "$MIG_DIR/timing.sh"
  local start fails=0 secret_ids=${REPOINT_SECRET_IDS:-$TARGET_SECRET_PREFIX/keycloak-customer-db $TARGET_SECRET_PREFIX/accounts-db}
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
    for d in KUBECONFIG_TARGET KUBECONFIG_HUB KUBECONFIG_DEV; do [ -f "${!d}" ] || bad="$bad $d"; done
    [ -z "$bad" ] && echo "dirs 700, three kubeconfigs present" || { echo "missing or wrong mode:$bad"; return 1; }
  }
  runner() { [ "$(ktarget -n thundermail get pod runner -o jsonpath='{.status.phase}')" = Running ] && echo Running; }
  code() { curl -sk -m 15 -o /dev/null -w '%{http_code}' "$@"; }
  http200() { local c; c=$(code "$@"); [ "$c" = 200 ] && echo "HTTP 200" || { echo "HTTP $c"; return 1; }; }
  shared_scope() {
    local tok s z; tok=$(sm_target "$CF_EXTERNAL_DNS_SECRET" | jq -r '.apiToken // empty')
    s=$(curl -s -m 20 -K <(bearer "$tok") "$CF_API/user/tokens/verify" | jq -r '.result.status // "error"')
    z=$(curl -s -m 20 -K <(bearer "$tok") "$CF_API/zones?name=$MAIL_ZONE" | jq -r '.result[0].id // empty')
    [ "$s" = active ] && [ -n "$z" ] && { [ -z "$CF_ZONE_ID" ] || [ "$z" = "$CF_ZONE_ID" ]; } && echo "active, zone visible${CF_ZONE_ID:+ and equal to CF_ZONE_ID}" || { echo "status=$s zone_visible=$([ -n "$z" ] && echo yes || echo no)"; return 1; }
  }
  agent_token() { local s; s=$(curl -s -m 20 -K <(bearer "$CF_API_TOKEN_AGENT") "$CF_API/user/tokens/verify" | jq -r '.result.status // "error"'); [ "$s" = active ] && [ -n "$CF_ZONE_ID" ] && [ -n "$CF_ZONE_ID_TBPRO" ] && echo "active, $MAIL_ZONE and $TBPRO_ZONE zone ids set" || { echo "status=$s"; return 1; }; }
  spf_final() { if [ "$SPF_FINAL_TXT" = "$SPF_DECIDED_TXT" ]; then echo "equals the #1535 text, len=${#SPF_FINAL_TXT}"; else echo "SPF_FINAL_TXT is not the #1535 text '$SPF_DECIDED_TXT' (len=${#SPF_FINAL_TXT})"; return 1; fi; }
  pr_pair() {
    local repo=thunderbird/$1 pr=$2 rev=$3 head base
    [ -n "$pr" ] && [ -n "$rev" ] || { echo "PR numbers empty"; return 1; }
    head=$(gh pr view "$pr" -R "$repo" --json state,headRefName -q 'select(.state == "OPEN") | .headRefName')
    base=$(gh pr view "$rev" -R "$repo" --json state,baseRefName -q 'select(.state == "OPEN") | .baseRefName')
    [ -n "$head" ] && [ "$base" = "$head" ] && { echo "#$pr open, revert #$rev open on its head"; return 0; }
    [ -z "$head" ] && [ "$base" = main ] && [ "$(gh pr view "$pr" -R "$repo" --json state -q .state)" = MERGED ] && { echo "#$pr merged, revert #$rev open on main"; return 0; }
    echo "#$pr head=${head:-not open}, revert #$rev base=${base:-not open}"; return 1
  }
  no_stage() {
    local n bad=""
    for n in $(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$ENV_FILE" | sort -u); do [[ ${!n,,} != *stage* ]] || bad="$bad $n"; done
    [ -z "$bad" ] && echo "no value contains stage" || { echo "values contain stage:$bad"; return 1; }
  }
  posint() { [[ ${!1:-} =~ ^[1-9][0-9]*$ ]] && echo "$1=${!1}" || { echo "$1='${!1:-}' is not a positive integer"; return 1; }; }
  lane_limit() { if [ -z "${!1:-}" ] && [ "${REHEARSAL:-0}" = 1 ]; then echo "$1 unset: no limit (REHEARSAL=1)"; else posint "$1"; fi; }
  nlb_live() {
    local h st
    h=$(ktarget -n thundermail get svc nginx -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)
    st=$([ -n "$h" ] && aws_target elbv2 describe-load-balancers --query "LoadBalancers[?DNSName=='$h'].State.Code | [0]" --output text 2>/dev/null)
    [ -n "$h" ] && [ "$h" = "${TARGET_NLB_DNS:-}" ] && [ "$st" = active ] && echo "TARGET_NLB_DNS matches the live nginx Service, NLB active" || { echo "env=${TARGET_NLB_DNS:-empty} live=${h:-none} state=${st:-none}: rebuild the env file"; return 1; }
  }
  target_health() { [ -n "${STALWART_URL:-}" ] || { echo "STALWART_URL unset: set LANE_PF_PORT"; return 1; }; http200 "$STALWART_URL/healthz/live"; }
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
  chk stalwart-target-healthz target_health
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
  if [ "$TARGET_ENV" = tb-prod ]; then
    chk no-stage-values no_stage
    chk watch-interval posint WATCH_INTERVAL
    chk lane-report-max lane_limit LANE_REPORT_MAX
    chk lane-wait-max lane_limit LANE_WAIT_MAX
    chk t7-pr-replicas held_replicas "${KCD_DIR:-}" "${T7_PR:-}" StatefulSet keycloak-customer "${KEYCLOAK_REPLICAS:-}"
    chk t8-pr-replicas held_replicas "${ACD_DIR:-}" "${T8_PR:-}" Deployment accounts-web "${ACCOUNTS_WEB_REPLICAS:-}"
  fi
  local end; end=$(date -u +%FT%TZ)
  printf 'env-check\t%s\t%s\tfailures\t%s\n' "$start" "$end" "$fails" >> "$TIMING_TSV"
  printf 'env-check start=%s end=%s failures=%s (row appended to %s)\n' "$start" "$end" "$fails" "$TIMING_TSV"
  [ "$fails" = 0 ]
}

case "$MODE" in
  build) build ;;
  check) check ;;
esac
