#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ZONE=${1:-}
[ -n "$ZONE" ] || die "usage: snapshot-zone.sh ZONE_ID"
need CF_API_TOKEN_AGENT
zone_name=$(cf_api "$CF_API/zones/$ZONE" | jq -re '.result.name')
out=$EVIDENCE_DIR/zone-before-$ZONE-$TS.json
doh=$EVIDENCE_DIR/zone-doh-before-$ZONE-$TS.txt
tmp=$(mktemp "$EVIDENCE_DIR/.zone.XXXXXX")
trap 'rm -f "$tmp"' EXIT
page=1
: > "$tmp"
while :; do
  resp=$(cf_api "$CF_API/zones/$ZONE/dns_records?per_page=5000&page=$page")
  echo "$resp" | jq -e '.success' >/dev/null || die "dns_records page $page failed: $(echo "$resp" | jq -c '.errors')"
  echo "$resp" | jq -c '.result[]' >> "$tmp"
  pages=$(echo "$resp" | jq -r '.result_info.total_pages // 1')
  [ "$page" -ge "$pages" ] && break
  page=$((page + 1))
done
jq -s 'map({id,name,type,content,data,priority,ttl,proxied,comment})' "$tmp" > "$out"
count=$(jq 'length' "$out")
: > "$doh"
jq -r 'map(select(.type | IN("A","AAAA","CNAME","MX","TXT","SRV","TLSA","CAA","NS"))) | map("\(.name) \(.type)") | unique | .[]' "$out" | head -400 | while read -r name type; do
  ans=$(curl -s -m 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$name&type=$type" | jq -c '[.Answer[]? | select(.type != 46) | .data] | sort')
  printf '%s\t%s\t%s\n' "$name" "$type" "${ans:-[]}" >> "$doh"
done
printf 'zone=%s name=%s records=%s doh_lines=%s\n%s\n%s\n' "$ZONE" "$zone_name" "$count" "$(wc -l < "$doh")" "$doh" "$out"
