#!/usr/bin/env bash
set -euo pipefail

APPT_HOST=${APPT_HOST:-appointment-stage.tb.pro}
AUTH_HOST=${AUTH_HOST:-auth.stage-thundermail.com}
ACCOUNTS_HOST=${ACCOUNTS_HOST:-accounts.stage-thundermail.com}
CALDAV_HOST=${CALDAV_HOST:-stage-thundermail.com}
OLD_AUTH_HOST=auth-stage.tb.pro
OLD_ACCOUNTS_HOST=accounts-stage.tb.pro
OIDC_CLIENT_ID=thunderbird-appointment-frontend

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
fail=0
skipped=0

doh() {
  curl -s -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=${2:-A}" |
    python3 -I -c 'import sys,json;d=json.load(sys.stdin);t={"A":1,"SRV":33}[sys.argv[1]];print(" ".join(x["data"] for x in d.get("Answer",[]) if x["type"]==t))' "${2:-A}"
}

resolve_arg() {
  local host=$1 port=${2:-443} ip
  ip=$(doh "$host" | cut -d' ' -f1)
  if [ -z "$ip" ]; then echo "FAIL dns $host has no A record" >&2; fail=1; return 1; fi
  echo "--resolve $host:$port:$ip"
}

check() {
  local name=$1 got=$2 want=$3
  if [[ "$got" =~ ^($want)$ ]]; then echo "PASS $name $got"; else echo "FAIL $name got=$got want=$want"; fail=1; fi
}

contains() {
  local name=$1 file=$2 needle=$3
  if grep -qF -- "$needle" "$file"; then echo "PASS $name present"; else echo "FAIL $name missing"; fail=1; fi
}

absent() {
  local name=$1 file=$2 needle=$3
  if grep -qF -- "$needle" "$file"; then echo "FAIL $name still present"; fail=1; else echo "PASS $name absent"; fi
}

echo "## frontend bundle on https://$APPT_HOST"
APPT_RES=$(resolve_arg "$APPT_HOST") || fail=1
curl -sf $APPT_RES "https://$APPT_HOST/" -o "$TMP/index.html" || { echo "FAIL page download https://$APPT_HOST/"; fail=1; }
mapfile -t ASSETS < <(grep -oE '(src|href)="[^"]+\.js"' "$TMP/index.html" | sed -E 's/^[a-z]+="//; s/"$//' | sort -u)
echo "assets: ${#ASSETS[@]}"
: > "$TMP/bundle.js"
for a in "${ASSETS[@]}"; do
  case "$a" in https://*) url=$a ;; /*) url="https://$APPT_HOST$a" ;; *) url="https://$APPT_HOST/$a" ;; esac
  curl -sf $APPT_RES "$url" >> "$TMP/bundle.js" || { echo "FAIL bundle download $url"; fail=1; }
done
contains "oidc root (new)" "$TMP/bundle.js" "https://$AUTH_HOST/realms/tbpro/"
contains "dashboard url (new)" "$TMP/bundle.js" "https://$ACCOUNTS_HOST/dashboard"
contains "support url (new)" "$TMP/bundle.js" "https://$ACCOUNTS_HOST/contact"
absent "old auth host" "$TMP/bundle.js" "$OLD_AUTH_HOST"
absent "old accounts host" "$TMP/bundle.js" "$OLD_ACCOUNTS_HOST"

echo "## login flow"
AUTH_RES=$(resolve_arg "$AUTH_HOST") || fail=1
DISC="https://$AUTH_HOST/realms/tbpro/.well-known/openid-configuration"
check "discovery GET" "$(curl -s $AUTH_RES -o "$TMP/disc.json" -w '%{http_code}' "$DISC")" 200
ISSUER=$(python3 -I -c 'import sys,json;print(json.load(open(sys.argv[1])).get("issuer",""))' "$TMP/disc.json" 2>/dev/null || true)
check "issuer host" "$ISSUER" "https://$AUTH_HOST/realms/tbpro"
AUTHZ=$(python3 -I -c 'import sys,json;print(json.load(open(sys.argv[1])).get("authorization_endpoint",""))' "$TMP/disc.json" 2>/dev/null || true)
CHALLENGE=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')
check "authorize GET (login page)" "$(curl -s $AUTH_RES -o /dev/null -w '%{http_code}' "$AUTHZ?client_id=$OIDC_CLIENT_ID&redirect_uri=https://$APPT_HOST/post-login/&response_type=code&scope=openid&state=p1&code_challenge=$CHALLENGE&code_challenge_method=S256")" 200
check "api root" "$(curl -s $APPT_RES -o /dev/null -w '%{http_code}' "https://$APPT_HOST/api/v1/")" 200
check "api /me with bad token (introspection reachable)" "$(curl -s $APPT_RES -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer p1-invalid' "https://$APPT_HOST/api/v1/me")" '401|403'

echo "## caldav connect"
if [ -n "${P1_OIDC_ACCESS_TOKEN:-}" ]; then
  printf 'header = "Authorization: Bearer %s"\n' "$P1_OIDC_ACCESS_TOKEN" > "$TMP/auth.cfg"
  check "api /me with token" "$(curl -s $APPT_RES -K "$TMP/auth.cfg" -o /dev/null -w '%{http_code}' "https://$APPT_HOST/api/v1/me")" 200
  check "caldav/oidc/auth (new app password via Accounts)" "$(curl -s $APPT_RES -K "$TMP/auth.cfg" -o /dev/null -w '%{http_code}' -X POST "https://$APPT_HOST/api/v1/caldav/oidc/auth")" 200
else
  echo "SKIP caldav/oidc/auth: set P1_OIDC_ACCESS_TOKEN (browser login token for a stage test user)"; skipped=$((skipped+1))
fi
if [ -n "${P1_CALDAV_USER:-}" ] && [ -n "${P1_CALDAV_APP_PASSWORD:-}" ]; then
  SRV=$(doh "_caldavs._tcp.$CALDAV_HOST" SRV | awk 'NF >= 4 {print $4":"$3}' | head -1 | sed 's/\.:/:/')
  if [ -n "$SRV" ]; then CD_HOST=${SRV%%:*}; CD_PORT=${SRV##*:}; else CD_HOST=$CALDAV_HOST; CD_PORT=443; fi
  CD_RES=$(resolve_arg "$CD_HOST" "$CD_PORT") || fail=1
  printf 'user = "%s:%s"\n' "$P1_CALDAV_USER" "$P1_CALDAV_APP_PASSWORD" > "$TMP/caldav.cfg"
  check "caldav PROPFIND $CD_HOST:$CD_PORT" "$(curl -s $CD_RES -K "$TMP/caldav.cfg" -L -o /dev/null -w '%{http_code}' -X PROPFIND -H 'Depth: 0' "https://$CD_HOST:$CD_PORT/.well-known/caldav")" '207|200|301|302'
else
  echo "SKIP caldav PROPFIND: set P1_CALDAV_USER and P1_CALDAV_APP_PASSWORD"; skipped=$((skipped+1))
fi

if [ "$fail" -ne 0 ]; then echo "P1 VERIFY: FAIL (skipped $skipped)"; exit "$fail"; fi
if [ "$skipped" -ne 0 ]; then echo "P1 VERIFY: INCOMPLETE (skipped $skipped)"; exit 2; fi
echo "P1 VERIFY: PASS"
