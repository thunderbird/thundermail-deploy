#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KC_LEGACY_PG_HOST KC_LEGACY_PG_USER
image=$(kstage -n keycloak-customer exec keycloak-customer-0 -- /opt/keycloak/bin/kc.sh --version 2>/dev/null | sed -n 's/^Keycloak \([0-9][0-9.]*\).*/\1/p' | head -1)
main=$(pod psql -h "$KC_LEGACY_PG_HOST" -U "$KC_LEGACY_PG_USER" -d "${KC_LEGACY_PG_DB:-keycloak}" -Atc 'select version from migration_model order by update_time desc limit 1')
printf 'image=%s migration_model=%s\n' "$image" "$main"
[ -n "$image" ] && [ "$image" = "$main" ] && echo 'GATE PASS' || { echo 'GATE FAIL: versions differ'; exit 1; }
