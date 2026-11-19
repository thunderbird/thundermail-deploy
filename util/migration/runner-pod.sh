#!/usr/bin/env bash
set -euo pipefail
SECRET=0
for a in "$@"; do
  case "$a" in
    --secret) SECRET=1 ;;
    -h|--help) echo 'usage: TARGET_ENV=<env> runner-pod.sh [--secret]'; exit 0 ;;
    *) echo "usage: TARGET_ENV=<env> runner-pod.sh [--secret] (unknown argument: $a)" >&2; exit 64 ;;
  esac
done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
for v in RUNNER_SGP_LABEL ${RUNNER_WORK_SIZE+RUNNER_WORK_SIZE} LEGACY_PG_SECRET_NAME; do
  [ -n "${!v}" ] && no_placeholders "$v" 2>/dev/null || die "$v in targets/$TARGET_ENV.sh is '${!v}': fill it before rendering the runner"
done
declare -A PFX=([stalwart]=STALWART_LEGACY_PG [keycloak]=KC_LEGACY_PG [accounts]=ACCOUNTS_LEGACY_PG)
NEEDS=() HOSTS=() VALID=() EXCL=()
for k in ${RUNNER_LEGACY_PG_KEYS-stalwart keycloak accounts}; do
  [ -n "${PFX[$k]:-}" ] || die "RUNNER_LEGACY_PG_KEYS in targets/$TARGET_ENV.sh has '$k': only stalwart, keycloak and accounts are known"
  VALID+=("$k")
  NEEDS+=("${PFX[$k]}_HOST" "${PFX[$k]}_PASSWORD")
  HOSTS+=("$k-host")
done
KEYS=${VALID[*]}
[ -n "$KEYS" ] || die "RUNNER_LEGACY_PG_KEYS in targets/$TARGET_ENV.sh is empty"
[ "$TARGET_ENV" != tb-prod ] || [[ " $KEYS " != *" stalwart "* ]] || die "RUNNER_LEGACY_PG_KEYS in targets/tb-prod.sh has 'stalwart': the tb-prod runner gets no legacy Stalwart credential"
for k in stalwart keycloak accounts; do [[ " $KEYS " == *" $k "* ]] || EXCL+=("$k" "$k-host"); done
N=${#VALID[@]}
if [ "$SECRET" = 0 ]; then
  ed=(-e "s|^    app.kubernetes.io/name: stalwart-config-applier\$|    app.kubernetes.io/name: $RUNNER_SGP_LABEL|" -e "s|rehearsal-legacy-pg|$LEGACY_PG_SECRET_NAME|g")
  [ -z "${RUNNER_WORK_SIZE:-}" ] || ed+=(-e "/^    - name: work\$/,/sizeLimit:/ s|sizeLimit: .*|sizeLimit: $RUNNER_WORK_SIZE|")
  if [ "$KEYS" != "stalwart keycloak accounts" ]; then
    ed+=(-e "s|for k in stalwart keycloak accounts stalwart-host keycloak-host accounts-host;|for k in $KEYS ${HOSTS[*]};|" -e "s|for k in stalwart keycloak accounts;|for k in $KEYS;|g")
    ed+=(-e "/hosts are not three distinct hosts/ s| = 3 \]| = $N ]|" -e "s|hosts are not three distinct hosts|hosts are not $N distinct hosts|")
    ed+=(-e "/has no \\\$k key/ s#; done\$#; done; for k in ${EXCL[*]}; do [ ! -e \"/etc/pg/legacy/\$k\" ] || { echo \"$LEGACY_PG_SECRET_NAME has a \$k key\" >\\&2; exit 1; }; done#")
  fi
  sed "${ed[@]}" "$MIG_DIR/runner-pod.yaml"
  exit 0
fi
need "${NEEDS[@]}" SECRET_DIR
export "${NEEDS[@]}"
if [[ " $KEYS " != *" stalwart "* ]] && [ -n "${STALWART_LEGACY_PG_HOST:-}" ]; then
  for k in $KEYS; do h=${PFX[$k]}_HOST; [ "${!h}" != "$STALWART_LEGACY_PG_HOST" ] || die "$h equals STALWART_LEGACY_PG_HOST: its password line would reach the legacy Stalwart database"; done
fi
[ "$(for k in $KEYS; do h=${PFX[$k]}_HOST; printf '%s\n' "${!h}"; done | sort -u | grep -c .)" = "$N" ] || die "the ${N/3/three} legacy hosts are not distinct"
mkdir -p "$SECRET_DIR"
out=$SECRET_DIR/$LEGACY_PG_SECRET_NAME-$TS.yaml
jq -n --arg name "$LEGACY_PG_SECRET_NAME" --arg keys "$KEYS" \
  '{stalwart: "STALWART_LEGACY_PG", keycloak: "KC_LEGACY_PG", accounts: "ACCOUNTS_LEGACY_PG"} as $p
   | {apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: $name, namespace: "thundermail"},
      stringData: (reduce ($keys | split(" ")[]) as $k ({}; . + {($k): $ENV[$p[$k] + "_PASSWORD"], ($k + "-host"): $ENV[$p[$k] + "_HOST"]}))}' > "$out"
chmod 600 "$out"
echo "$out"
