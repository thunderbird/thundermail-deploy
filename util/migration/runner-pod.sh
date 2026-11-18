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
for v in RUNNER_SGP_LABEL RUNNER_WORK_SIZE LEGACY_PG_SECRET_NAME; do
  case "${!v}" in ''|*REPLACE_ME*) die "$v in targets/$TARGET_ENV.sh is '${!v}': fill it before rendering the runner" ;; esac
done
if [ "$SECRET" = 0 ]; then
  sed -e "s|^    app.kubernetes.io/name: stalwart-config-applier\$|    app.kubernetes.io/name: $RUNNER_SGP_LABEL|" \
    -e "s|rehearsal-legacy-pg|$LEGACY_PG_SECRET_NAME|g" \
    -e "/^    - name: work\$/,/sizeLimit:/ s|sizeLimit: .*|sizeLimit: $RUNNER_WORK_SIZE|" \
    "$MIG_DIR/runner-pod.yaml"
  exit 0
fi
need STALWART_LEGACY_PG_HOST STALWART_LEGACY_PG_PASSWORD KC_LEGACY_PG_HOST KC_LEGACY_PG_PASSWORD ACCOUNTS_LEGACY_PG_HOST ACCOUNTS_LEGACY_PG_PASSWORD SECRET_DIR
[ "$(printf '%s\n' "$STALWART_LEGACY_PG_HOST" "$KC_LEGACY_PG_HOST" "$ACCOUNTS_LEGACY_PG_HOST" | sort -u | grep -c .)" = 3 ] || die "the three legacy hosts are not distinct"
mkdir -p "$SECRET_DIR"
out=$SECRET_DIR/$LEGACY_PG_SECRET_NAME-$TS.yaml
jq -n --arg name "$LEGACY_PG_SECRET_NAME" \
  --arg s "$STALWART_LEGACY_PG_PASSWORD" --arg sh "$STALWART_LEGACY_PG_HOST" \
  --arg k "$KC_LEGACY_PG_PASSWORD" --arg kh "$KC_LEGACY_PG_HOST" \
  --arg a "$ACCOUNTS_LEGACY_PG_PASSWORD" --arg ah "$ACCOUNTS_LEGACY_PG_HOST" \
  '{apiVersion: "v1", kind: "Secret", type: "Opaque", metadata: {name: $name, namespace: "thundermail"},
    stringData: {stalwart: $s, "stalwart-host": $sh, keycloak: $k, "keycloak-host": $kh, accounts: $a, "accounts-host": $ah}}' > "$out"
chmod 600 "$out"
echo "$out"
