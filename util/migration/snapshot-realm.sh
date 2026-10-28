#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
URL=${1:-}
[ -n "$URL" ] || die "usage: snapshot-realm.sh KEYCLOAK_URL [REALM]"
REALM=${2:-tbpro}
CID=${KC_ADMIN_CLIENT_ID:-${KC_LEGACY_ADMIN_CLIENT_ID:-}}
CSEC=${KC_ADMIN_CLIENT_SECRET:-${KC_LEGACY_ADMIN_CLIENT_SECRET:-}}
[ -n "$CID" ] && [ -n "$CSEC" ] || die "set KC_LEGACY_ADMIN_CLIENT_ID/SECRET (or KC_ADMIN_CLIENT_ID/SECRET) in the environment"
mkdir -p "$SECRET_DIR"
token=$(curl -sS -m 30 -K <(printf 'data = "grant_type=client_credentials&client_id=%s&client_secret=%s"\n' "$CID" "$CSEC") "$URL/realms/master/protocol/openid-connect/token" | jq -re '.access_token')
auth=$(mktemp); trap 'rm -f "$auth"' EXIT
printf 'header = "Authorization: Bearer %s"\n' "$token" > "$auth"
out=$SECRET_DIR/realm-before-$TS.json
jq -n --argjson clients "$(curl -sS -m 60 -K "$auth" "$URL/admin/realms/$REALM/clients?max=1000" | jq 'map(del(.secret))')" \
      --argjson master "$(curl -sS -m 30 -K "$auth" "$URL/admin/realms/master" | jq '{attributes}')" \
      --arg url "$URL" --arg realm "$REALM" '{url: $url, realm: $realm, clients: $clients, master: $master}' > "$out"
printf 'url=%s realm=%s clients=%s master_frontendUrl=%s\n%s\n' "$URL" "$REALM" "$(jq '.clients|length' "$out")" "$(jq -r '.master.attributes.frontendUrl // "unset"' "$out")" "$out"
