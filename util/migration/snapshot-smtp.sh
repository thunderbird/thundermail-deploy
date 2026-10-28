#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
REALM=${1:-tbpro}
PORT=${KC_LOCAL_PORT:-18082}
CID=${KC_STAGE_ADMIN_CLIENT_ID:-tb-accounts-admin}
CSEC=$(kstage -n keycloak-customer get secret keycloak-customer-admin-client -o jsonpath='{.data.client-secret}' | base64 -d)
[ -n "$CSEC" ] || die "keycloak-customer-admin-client secret has no client-secret"
kstage -n keycloak-customer port-forward pod/keycloak-customer-0 "$PORT:8080" >/dev/null 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -sf -o /dev/null "http://localhost:$PORT/realms/master" && break; sleep 1; done
token=$(curl -sS -m 30 -K <(printf 'data = "grant_type=client_credentials&client_id=%s&client_secret=%s"\n' "$CID" "$CSEC") "http://localhost:$PORT/realms/master/protocol/openid-connect/token" | jq -re '.access_token')
out=$EVIDENCE_DIR/smtp-before-$TS.json
curl -sS -m 30 -K <(printf 'header = "Authorization: Bearer %s"\n' "$token") "http://localhost:$PORT/admin/realms/$REALM" | jq '{smtpServer: (.smtpServer | del(.password))}' > "$out"
printf 'realm=%s host=%s from=%s envelopeFrom=%s user=%s\n%s\n' "$REALM" "$(jq -r '.smtpServer.host // "unset"' "$out")" "$(jq -r '.smtpServer.from // "unset"' "$out")" "$(jq -r '.smtpServer.envelopeFrom // "unset"' "$out")" "$(jq -r '.smtpServer.user // "unset"' "$out")" "$out"
