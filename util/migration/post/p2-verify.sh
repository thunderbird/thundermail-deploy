#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../target.sh"
stage_only
REDIRECTS=${REDIRECTS:-"$LEGACY_ACCOUNTS_HOST=https://$ACCOUNTS_HOST $LEGACY_AUTH_HOST=https://$AUTH_HOST"}
TRIES=${TRIES:-5}
fail=0

doh() {
  curl -s -m 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=A" |
    python3 -I -c 'import sys,json;print(" ".join(x["data"] for x in json.load(sys.stdin).get("Answer",[]) if x["type"]==1))'
}

for r in $REDIRECTS; do
  host=${r%%=*} want="301 ${r#*=}/login/?p2=1" got=""
  for i in $(seq "$TRIES"); do
    ip=$(doh "$host" 2>/dev/null | cut -d' ' -f1 || true)
    [ -z "$ip" ] || got=$(curl -s -m 15 --resolve "$host:443:$ip" -o /dev/null -w '%{http_code} %{redirect_url}' "https://$host/login/?p2=1" || true)
    [ "$got" != "$want" ] || break
    [ "$i" = "$TRIES" ] || sleep 10
  done
  if [ "$got" = "$want" ]; then echo "PASS $host $got"; else echo "FAIL $host got='${got:-no answer}' want='$want'"; fail=1; fi
done

if [ "$fail" -ne 0 ]; then echo "P2 VERIFY: FAIL"; exit 1; fi
echo "P2 VERIFY: PASS"
