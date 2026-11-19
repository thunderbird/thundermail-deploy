#!/usr/bin/env bash
set -euo pipefail
DRY=0
PULL=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --pull) PULL=sts ;;
    --pull=*) PULL=${a#--pull=} ;;
  esac
done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KC_LEGACY_PG_HOST KC_LEGACY_PG_USER
query='select version from migration_model order by update_time desc limit 1'
sts_image() {
  local image
  image=$(ktarget -n keycloak-customer get sts keycloak-customer -o jsonpath='{.spec.template.spec.containers[?(@.name=="keycloak")].image}')
  [ -n "$image" ] || die "no keycloak container image on sts/keycloak-customer"
  printf '%s' "$image"
}
kc_version() {
  local err rc=0 out v
  [ "$TARGET_ENV" != tb-prod ] || podman image exists "$1" 2>/dev/null || echo "pulling $1 (not in the local podman store)" >&2
  err=$(mktemp)
  out=$(podman run --rm --entrypoint /opt/keycloak/bin/kc.sh "$1" --version 2>"$err") || rc=$?
  v=$(awk '$1 == "Keycloak" {print $2; exit}' <<<"$out")
  if [ "$rc" != 0 ] || [ -z "$v" ]; then
    echo "GATE FAIL: podman run of $1 gave no Keycloak version (rc=$rc): $(grep . "$err" | tail -1)" >&2
    rm -f "$err"
    return 1
  fi
  rm -f "$err"
  printf '%s\n' "$v"
}
image_version() {
  [ -z "${KC_IMAGE_VERSION:-}" ] || { printf '%s\n' "$KC_IMAGE_VERSION"; return 0; }
  kc_version "$(sts_image)"
}
if [ -n "$PULL" ]; then
  [ "$PULL" != sts ] || PULL=$(sts_image)
  v=$(kc_version "$PULL") || exit 1
  printf 'image %s cached: Keycloak %s\n' "$PULL" "$v"
  exit 0
fi
if [ "$DRY" = 1 ]; then
  printf 'dry-run: pod psql -h %s -U <%s chars> -d %s -Atc "%s"\n' "$KC_LEGACY_PG_HOST" "${#KC_LEGACY_PG_USER}" "${KC_LEGACY_PG_DB:-keycloak}" "$query"
  printf 'dry-run: image version from %s\n' "${KC_IMAGE_VERSION:+KC_IMAGE_VERSION=$KC_IMAGE_VERSION}${KC_IMAGE_VERSION:-podman kc.sh --version on the sts/keycloak-customer image (no pod exec)}"
  exit 0
fi
image=$(image_version) || exit 1
main=$(pod psql -h "$KC_LEGACY_PG_HOST" -U "$KC_LEGACY_PG_USER" -d "${KC_LEGACY_PG_DB:-keycloak}" -Atc "$query")
printf 'image=%s migration_model=%s\n' "$image" "$main"
[ -n "$image" ] && [ "$image" = "$main" ] && echo 'GATE PASS' || { echo 'GATE FAIL: versions differ'; exit 1; }
