#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ZONE=${1:-}; OP=${2:-}; NAME=${3:-}; TYPE=${4:-}; CONTENT=${5:-}
case "$OP" in
  upsert) [ -n "$CONTENT" ] || die "usage: cf-record.sh ZONE_ID upsert NAME TYPE CONTENT [--apply] (empty CONTENT refused)" ;;
  delete) ;;
  *) die "usage: cf-record.sh ZONE_ID upsert|delete NAME TYPE [CONTENT] [--apply]" ;;
esac
if [ -z "$ZONE" ] || [ -z "$NAME" ] || [ -z "$TYPE" ]; then die "usage: cf-record.sh ZONE_ID upsert|delete NAME TYPE [CONTENT] [--apply]"; fi
case "$NAME" in _externaldns.*) die "refusing to touch $NAME (external-dns owner record)" ;; esac
case "$OP:$NAME" in delete:spf.*) die "refusing to delete $NAME: custom domains include it; upsert it instead" ;; esac
need CF_API_TOKEN_AGENT
unq() { sed 's/^"//; s/"$//'; }
ok() { jq -e '.success' <<<"$1" >/dev/null || die "Cloudflare API: $(jq -c '.errors' <<<"$1")"; }

resp=$(cf_api "$CF_API/zones/$ZONE/dns_records?type=$TYPE&name=$NAME&per_page=100")
ok "$resp"
recs=$(jq -c '.result[] | {id, content, ttl, proxied}' <<<"$resp")
spf=0
[ "$TYPE" = TXT ] && [[ "$CONTENT" == v=spf1* ]] && spf=1
if [ "$OP" = delete ]; then
  n=$(grep -c . <<<"$recs" || true)
  [ "$n" -gt 0 ] || { printf 'mode=%s %s %s: nothing to delete\n' "$(mode)" "$TYPE" "$NAME"; exit 0; }
  while IFS= read -r r; do
    printf 'DELETE %s %s %s\n' "$TYPE" "$NAME" "$(jq -r .content <<<"$r" | unq)"
    [ "$APPLY" = 1 ] || continue
    ok "$(cf_api -X DELETE "$CF_API/zones/$ZONE/dns_records/$(jq -r .id <<<"$r")")"
  done <<<"$recs"
  printf 'mode=%s deleted=%s\n' "$(mode)" "$n"
  exit 0
fi
if [ "$spf" = 1 ]; then
  match=$(jq -c 'select(.content | ltrimstr("\"") | startswith("v=spf1"))' <<<"$recs")
elif [ "$TYPE" = TXT ]; then
  match=$(jq -c --arg c "$CONTENT" 'select((.content | ltrimstr("\"") | rtrimstr("\"")) == $c)' <<<"$recs")
else
  match=$recs
fi
n=$(grep -c . <<<"$match" || true)
[ "$n" -le 1 ] || die "$n $TYPE records at $NAME match: resolve by hand first"
body=$(jq -cn --arg t "$TYPE" --arg n "$NAME" --arg c "$CONTENT" '{type: $t, name: $n, content: $c, ttl: 300, proxied: false}')
if [ "$n" = 0 ]; then
  printf 'CREATE %s %s %s\n' "$TYPE" "$NAME" "$CONTENT"
  [ "$APPLY" = 0 ] || ok "$(cf_api -X POST "$CF_API/zones/$ZONE/dns_records" --data "$body")"
else
  old=$(jq -r .content <<<"$match" | unq)
  if [ "$old" = "$CONTENT" ]; then printf 'mode=%s %s %s already %s\n' "$(mode)" "$TYPE" "$NAME" "$CONTENT"; exit 0; fi
  printf 'UPDATE %s %s %s -> %s\n' "$TYPE" "$NAME" "$old" "$CONTENT"
  [ "$APPLY" = 0 ] || ok "$(cf_api -X PATCH "$CF_API/zones/$ZONE/dns_records/$(jq -r .id <<<"$match")" --data "$(jq -c 'del(.ttl, .proxied)' <<<"$body")")"
fi
if [ "$APPLY" = 1 ]; then
  resp=$(cf_api "$CF_API/zones/$ZONE/dns_records?type=$TYPE&name=$NAME&per_page=100")
  ok "$resp"
  jq -r '.result[] | .content' <<<"$resp" | unq | grep -qxF -- "$CONTENT" || die "read back: $TYPE $NAME does not hold the new content"
fi
printf 'mode=%s %s %s %s\n' "$(mode)" "$OP" "$TYPE" "$NAME"
