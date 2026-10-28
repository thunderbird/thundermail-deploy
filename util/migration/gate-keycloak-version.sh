#!/usr/bin/env bash
set -euo pipefail
DRY=0
for a in "$@"; do [ "$a" = --dry-run ] && DRY=1; done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KC_LEGACY_PG_HOST KC_LEGACY_PG_USER
query='select version from migration_model order by update_time desc limit 1'
image_version() {
  [ -z "${KC_IMAGE_VERSION:-}" ] || { printf '%s\n' "$KC_IMAGE_VERSION"; return 0; }
  local image
  image=$(kstage -n keycloak-customer get sts keycloak-customer -o jsonpath='{.spec.template.spec.containers[?(@.name=="keycloak")].image}')
  [ -n "$image" ] || die "no keycloak container image on sts/keycloak-customer"
  podman run --rm --entrypoint /opt/keycloak/bin/kc.sh "$image" --version 2>/dev/null | awk '$1 == "Keycloak" {print $2; exit}'
}
if [ "$DRY" = 1 ]; then
  printf 'dry-run: pod psql -h %s -U <%s chars> -d %s -Atc "%s"\n' "$KC_LEGACY_PG_HOST" "${#KC_LEGACY_PG_USER}" "${KC_LEGACY_PG_DB:-keycloak}" "$query"
  printf 'dry-run: image version from %s\n' "${KC_IMAGE_VERSION:+KC_IMAGE_VERSION=$KC_IMAGE_VERSION}${KC_IMAGE_VERSION:-podman kc.sh --version on the sts/keycloak-customer image (no pod exec)}"
  exit 0
fi
image=$(image_version)
main=$(pod psql -h "$KC_LEGACY_PG_HOST" -U "$KC_LEGACY_PG_USER" -d "${KC_LEGACY_PG_DB:-keycloak}" -Atc "$query")
printf 'image=%s migration_model=%s\n' "$image" "$main"
[ -n "$image" ] && [ "$image" = "$main" ] && echo 'GATE PASS' || { echo 'GATE FAIL: versions differ'; exit 1; }
