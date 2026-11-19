#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
USAGE="usage: TARGET_ENV=tb-prod runner-start.sh [start | check | remove] [--dry-run | --apply]"
[ "${#POS[@]}" -le 1 ] || { echo "$USAGE" >&2; exit 64; }
MODE=${POS[0]:-start}
case "$MODE" in start|check|remove) ;; *) echo "$USAGE (unknown mode: $MODE)" >&2; exit 64 ;; esac
[ "$TARGET_ENV" = tb-prod ] || { echo "runner-start.sh starts the tb-prod runner only, not $TARGET_ENV: on $TARGET_ENV apply the runner-pod.sh render as before" >&2; exit 64; }
need KUBECONFIG_TARGET SECRET_DIR TIMING_TSV
case "$(kube_cluster_of "$KUBECONFIG_TARGET" || true)" in
  "$TARGET_CLUSTER"|*/"$TARGET_CLUSTER") ;;
  *) echo "refusing: the current-context of KUBECONFIG_TARGET $KUBECONFIG_TARGET is not cluster $TARGET_CLUSTER" >&2; exit 64 ;;
esac
[ "$MODE" = remove ] || need STALWART_TARGET_PG_HOST STALWART_TARGET_PG_USER STALWART_TARGET_PG_DB STALWART_LEGACY_PG_HOST \
  KC_LEGACY_PG_HOST KC_LEGACY_PG_USER ACCOUNTS_LEGACY_PG_HOST ACCOUNTS_LEGACY_PG_USER ACCOUNTS_LEGACY_PG_NAME
K=("${KS[@]}" -n thundermail)
PX=("${K[@]}" exec -i runner -c postgres --)
SECRET_FILE=$SECRET_DIR/$LEGACY_PG_SECRET_NAME-$TS.yaml
MANIFEST=$EVIDENCE_DIR/runner-pod-$TS.yaml
LIVE=0
if [ "$APPLY" = 1 ] || [ "$MODE" = check ]; then LIVE=1; fi
FAILS=0
POD_OK=0
exec 3>&1
printf 'bash %s remove --apply\n' "$0" > "$RESTORE"

have() { printf 'HAVE  %s\n' "$*"; }
lack() { printf 'NEED  %s\n' "$*"; NEEDS=$((NEEDS + 1)); }
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
rd() { printf '  + %s\n' "$(shq "$@")" >&3; [ "$LIVE" = 1 ] || return 0; "$@"; }

gate_session() {
  local at left
  at=$(cat "$EVIDENCE_DIR/sso-login-at.txt" 2>/dev/null) || at=""
  [[ $at =~ ^[0-9]+$ ]] || { lack "no $EVIDENCE_DIR/sso-login-at.txt: run login of the window shell first"; return 0; }
  left=$(( (at + 14400 - $(date +%s)) / 60 ))
  if [ "$left" -ge 45 ]; then have "session minutes left: $left"; else lack "session minutes left: $left, under 45: run login first"; fi
}

gate_sgp() {
  local j mine srv
  j=$("${K[@]}" get securitygrouppolicies.vpcresources.k8s.aws -o json) || { lack "SecurityGroupPolicies in thundermail not readable"; return 0; }
  mine=$(jq -r --arg l "$RUNNER_SGP_LABEL" '[.items[] | select(.spec.podSelector.matchLabels == {"app.kubernetes.io/name": $l})] | map(.metadata.name + "=" + (.spec.securityGroups.groupIds | sort | join(","))) | join(" ")' <<<"$j")
  srv=$(jq -r '[.items[] | select(.spec.podSelector.matchLabels == {"app": "stalwart"}) | .spec.securityGroups.groupIds | sort | join(",")] | first // ""' <<<"$j")
  [ -n "$mine" ] || { lack "no SecurityGroupPolicy in thundermail selects app.kubernetes.io/name=$RUNNER_SGP_LABEL: the runner would get no security group that reaches Neon; merge the tb-prod settings PR that adds stalwart-config-applier"; return 0; }
  case "$mine" in
    *" "*) lack "more than one SecurityGroupPolicy selects app.kubernetes.io/name=$RUNNER_SGP_LABEL: ${mine// /, }" ;;
    *"=$srv") [ -n "$srv" ] && have "SecurityGroupPolicy ${mine%%=*} gives the runner the groups of the app=stalwart policy ($srv)" || lack "no SecurityGroupPolicy selects app=stalwart to compare with" ;;
    *) lack "SecurityGroupPolicy ${mine%%=*} has groups ${mine#*=}, not those of the app=stalwart policy (${srv:-none})" ;;
  esac
}

gate_target() {
  local k h
  k=$("${K[@]}" get secret stalwart-postgresql-admin-password -o go-template='{{if index .data "STALWART_POSTGRESQL_ADMIN_PASSWORD"}}yes{{end}}' 2>/dev/null) || k=""
  [ "$k" = yes ] && have "Secret stalwart-postgresql-admin-password has key STALWART_POSTGRESQL_ADMIN_PASSWORD" || lack "Secret stalwart-postgresql-admin-password has no STALWART_POSTGRESQL_ADMIN_PASSWORD key: the staged secrets come first"
  h=$("${K[@]}" get configmap stalwart-config -o jsonpath='{.data.config\.json}' 2>/dev/null | sed -n 's/.*"host": *"\([^"]*\)".*/\1/p' | head -1) || h=""
  if [ -z "$h" ]; then lack "ConfigMap stalwart-config has no database host"
  elif [ "${h/-pooler/}" = "$STALWART_TARGET_PG_HOST" ]; then have "ConfigMap stalwart-config names STALWART_TARGET_PG_HOST: the password file gets the cutover branch host"
  else lack "ConfigMap stalwart-config names another host than STALWART_TARGET_PG_HOST: merge the Stalwart database PR or rebuild the env file"; fi
}

gate_absent() {
  local p s
  p=$("${K[@]}" get pod runner --ignore-not-found -o name) || p=ERR
  s=$("${K[@]}" get secret "$LEGACY_PG_SECRET_NAME" --ignore-not-found -o name) || s=ERR
  [ -z "$p" ] && have "no runner pod" || lack "runner pod: ${p}: its password file is built at start; bash $0 remove --apply first"
  [ -z "$s" ] && have "no Secret $LEGACY_PG_SECRET_NAME" || lack "Secret $LEGACY_PG_SECRET_NAME: ${s}: bash $0 remove --apply first"
}

render() {
  local err
  if err=$(bash "$MIG_DIR/runner-pod.sh" 2>&1 >"$MANIFEST.tmp"); then
    sed '/^spec:$/a\  automountServiceAccountToken: false' "$MANIFEST.tmp" > "$MANIFEST"
    rm -f "$MANIFEST.tmp"
    have "runner manifest $MANIFEST"
  else
    rm -f "$MANIFEST.tmp"
    lack "runner-pod.sh: $err"
  fi
}

start() {
  trap 'rm -f "$SECRET_FILE"' EXIT
  run env SECRET_DIR="$SECRET_DIR" TS="$TS" bash "$MIG_DIR/runner-pod.sh" --secret || return 1
  run "${K[@]}" create -f "$SECRET_FILE" || { rm -f "$SECRET_FILE"; return 1; }
  run rm -f "$SECRET_FILE"
  run "${K[@]}" create -f "$MANIFEST" || return 1
  run "${K[@]}" wait --for=condition=Ready pod/runner --timeout=300s
}

admitted() {
  local j n r
  j=$(rd "${K[@]}" get pod runner -o json) || { fail "no runner pod"; return 0; }
  [ "$LIVE" = 1 ] || return 0
  POD_OK=1
  n=$(jq -r '.spec.nodeName // ""' <<<"$j")
  r=$( [ -z "$n" ] || rd "${KS[@]}" get node "$n" -o jsonpath='{.metadata.labels.role}') || r=""
  [ "$r" = mail ] && pass "node role mail" || fail "node ${n:-none} has role '${r}', not mail"
  [ "$(jq -r '[.spec.containers[].resources.limits["vpc.amazonaws.com/pod-eni"] // empty] | join(",")' <<<"$j")" = 1 ] && pass "vpc.amazonaws.com/pod-eni limit 1: a SecurityGroupPolicy matched" || fail "no vpc.amazonaws.com/pod-eni limit: no SecurityGroupPolicy matched the runner"
  [ -n "$(jq -r '.metadata.annotations["vpc.amazonaws.com/pod-eni"] // ""' <<<"$j")" ] && pass "branch network interface attached" || fail "no vpc.amazonaws.com/pod-eni annotation: no branch network interface"
  [ "$(jq -r '.spec.serviceAccountName // "default"' <<<"$j")" = default ] && pass "service account default" || fail "service account $(jq -r .spec.serviceAccountName <<<"$j"), not default"
  [ "$(jq -r '[.spec.automountServiceAccountToken, ([.spec.volumes[]? | select(.projected.sources[]?.serviceAccountToken)] | length)] | @tsv' <<<"$j")" = $'false\t0' ] && pass "no service account token" || fail "the runner mounts a service account token"
  [ "$(jq -r '[.status.containerStatuses[]? | select(.ready and .restartCount == 0)] | length' <<<"$j")" = 2 ] && pass "two containers ready, no restart" || fail "containers not both ready with 0 restarts: $(jq -c '[.status.containerStatuses[]? | {name, ready, restartCount}]' <<<"$j")"
}

login() {
  local name=$1 want=$2 got; shift 2
  got=$(rd "${PX[@]}" psql -w -h "$1" -U "$2" -d "$3" -v ON_ERROR_STOP=1 -Atc 'select current_database(), current_user' 2>&1) || got="error: $(tail -1 <<<"$got")"
  [ "$LIVE" = 1 ] || return 0
  [ "$got" = "$want" ] && pass "$name login: $got" || fail "$name login: $got, want $want"
}

logins() {
  local r c
  r=$(rd "${PX[@]}" pg_isready -h "$STALWART_LEGACY_PG_HOST" 2>&1) || true
  c=$(rd "${PX[@]}" sh -c 'awk -F: -v h="$1" '"'"'$1 == h {n++} END {print n + 0}'"'"' /secrets/.pgpass; test ! -e /etc/pg/legacy/stalwart && echo nokey' sh "$STALWART_LEGACY_PG_HOST" 2>&1) || true
  if [ "$LIVE" = 1 ]; then
    [[ $r == *"accepting connections"* ]] && pass "legacy Stalwart database: accepting connections" || fail "legacy Stalwart database: ${r##* - }"
    [ "$c" = $'0\nnokey' ] && pass "no legacy Stalwart password in the runner" || fail "the runner holds a legacy Stalwart password line or key"
  fi
  login cutover-branch "$STALWART_TARGET_PG_DB|$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_HOST" "$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_DB"
  login keycloak "${KC_LEGACY_PG_DB:-keycloak}|$KC_LEGACY_PG_USER" "$KC_LEGACY_PG_HOST" "$KC_LEGACY_PG_USER" "${KC_LEGACY_PG_DB:-keycloak}"
  login accounts "$ACCOUNTS_LEGACY_PG_NAME|$ACCOUNTS_LEGACY_PG_USER" "$ACCOUNTS_LEGACY_PG_HOST" "$ACCOUNTS_LEGACY_PG_USER" "$ACCOUNTS_LEGACY_PG_NAME"
}

zero_exception() {
  local p o
  p=$(rd "${K[@]}" get pods,jobs,cronjobs -o name) || { fail "thundermail pods, Jobs and CronJobs not readable"; return 0; }
  [ "$LIVE" = 1 ] || return 0
  o=$(grep -vx 'pod/runner' <<<"$p" | grep . | tr '\n' ' ') || o=""
  grep -qx 'pod/runner' <<<"$p" || { fail "zero: no pod/runner in thundermail"; return 0; }
  if [ -z "$o" ]; then pass "zero: thundermail 1 is pod/runner, no other pod, Job or CronJob"
  elif [ "$MODE" = start ]; then fail "zero: thundermail has pod/runner and ${o% }"
  else printf 'INFO  zero: thundermail has pod/runner and %s\n' "${o% }"; fi
}

remove() {
  run "${K[@]}" delete pod runner --ignore-not-found --wait=true --timeout=120s || return 1
  run "${K[@]}" delete secret "$LEGACY_PG_SECRET_NAME" --ignore-not-found || return 1
  run find "$SECRET_DIR" -maxdepth 1 -name "$LEGACY_PG_SECRET_NAME-*.yaml" -delete || return 1
  [ "$APPLY" = 1 ] || return 0
  [ -z "$(rd "${K[@]}" get pod runner --ignore-not-found -o name)" ] && [ -z "$(rd "${K[@]}" get secret "$LEGACY_PG_SECRET_NAME" --ignore-not-found -o name)" ] || { fail "runner pod or Secret $LEGACY_PG_SECRET_NAME still there"; return 0; }
  pass "runner pod and Secret $LEGACY_PG_SECRET_NAME gone"
}

checks() {
  if [ "$MODE" = start ] && [ "$APPLY" = 0 ]; then echo "== checks --apply runs once the pod is Ready"; else echo "== checks"; fi
  admitted
  [ "$LIVE" = 0 ] || [ "$POD_OK" = 1 ] || return 0
  logins
  zero_exception
}

case "$MODE" in
  start)
    echo "== gates"
    gate_session
    gate_sgp
    gate_target
    gate_absent
    render
    [ "$APPLY" = 0 ] || [ "$NEEDS" = 0 ] || stop "$NEEDS gate(s) NEED above"
    echo "== start"
    start || stop "runner start failed; the restore is $RESTORE"
    checks ;;
  check) checks ;;
  remove) echo "== remove"; remove || stop "remove failed" ;;
esac
[ "$FAILS" = 0 ] || stop "$FAILS check(s) FAIL above"
if [ "$APPLY" = 1 ]; then
  case "$MODE" in
    start) mark runner-start logins 3 ;;
    remove) mark runner-removed pod 0 ;;
  esac
fi
finish
