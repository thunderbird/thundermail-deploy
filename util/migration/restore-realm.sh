#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SNAP=${1:-}
[ -f "$SNAP" ] || die "usage: restore-realm.sh realm-before-<ts>.json [KEYCLOAK_URL] [--apply]"
jq -e '(.realm | type == "string") and (.clients | type == "array" and length > 0) and (.master | type == "object")' "$SNAP" >/dev/null || die "$SNAP is not a realm snapshot"
URL=${2:-$(jq -r '.url' "$SNAP")}
REALM=$(jq -r '.realm' "$SNAP")
CID=${KC_ADMIN_CLIENT_ID:-${KC_LEGACY_ADMIN_CLIENT_ID:-}}
CSEC=${KC_ADMIN_CLIENT_SECRET:-${KC_LEGACY_ADMIN_CLIENT_SECRET:-}}
[ -n "$CID" ] && [ -n "$CSEC" ] || die "set KC_LEGACY_ADMIN_CLIENT_ID/SECRET (or KC_ADMIN_CLIENT_ID/SECRET) in the environment"
token=$(curl -sS -m 30 -K <(printf 'data = "grant_type=client_credentials&client_id=%s&client_secret=%s"\n' "$CID" "$CSEC") "$URL/realms/master/protocol/openid-connect/token" | jq -re '.access_token')
auth=$(mktemp); trap 'rm -f "$auth"' EXIT
printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "$token" > "$auth"
norm='walk(if type == "array" then sort else . end)'
live=$(curl -sS -m 60 -K "$auth" "$URL/admin/realms/$REALM/clients?max=1000" | jq 'map(del(.secret)) | map({(.id): .}) | add // {}')
fail=0; n=0
while IFS= read -r client; do
  id=$(jq -r '.id' <<<"$client"); cid=$(jq -r '.clientId' <<<"$client")
  [ "$(jq -cS --arg id "$id" ".[\$id] | $norm" <<<"$live")" = "$(jq -cS "$norm" <<<"$client")" ] && continue
  n=$((n + 1))
  printf 'PUT client %s (%s)\n' "$cid" "$id"
  [ "$APPLY" = 1 ] || continue
  code=$(curl -sS -m 60 -o /dev/null -w '%{http_code}' -X PUT -K "$auth" "$URL/admin/realms/$REALM/clients/$id" --data-binary "$client")
  [ "$code" = 204 ] || { echo "  failed: HTTP $code"; fail=$((fail + 1)); }
done < <(jq -c '.clients[]' "$SNAP")
master=$(jq -c '.master' "$SNAP")
if [ "$(curl -sS -m 30 -K "$auth" "$URL/admin/realms/master" | jq -cS "{attributes} | $norm")" != "$(jq -cS "$norm" <<<"$master")" ]; then
  n=$((n + 1))
  echo 'PUT master realm attributes'
  if [ "$APPLY" = 1 ]; then
    code=$(curl -sS -m 60 -o /dev/null -w '%{http_code}' -X PUT -K "$auth" "$URL/admin/realms/master" --data-binary "$master")
    [ "$code" = 204 ] || { echo "  failed: HTTP $code"; fail=$((fail + 1)); }
  fi
fi
printf 'mode=%s url=%s realm=%s saved_clients=%s to_put=%s failed=%s\n' "$(mode)" "$URL" "$REALM" "$(jq '.clients|length' "$SNAP")" "$n" "$fail"
[ "$fail" = 0 ]
