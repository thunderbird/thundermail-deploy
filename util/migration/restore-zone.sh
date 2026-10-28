#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SNAP=${1:-}
[ -f "$SNAP" ] || die "usage: restore-zone.sh zone-before-<zone>-<ts>.json [--zone ZONE_ID] [--apply]"
ZONE=""
[ "${2:-}" = --zone ] && ZONE=${3:-}
[ -n "$ZONE" ] || ZONE=$(basename "$SNAP" | sed -n 's/^zone-before-\([0-9a-f]\{32\}\)-.*/\1/p')
[ -n "$ZONE" ] || die "zone id not in the file name; pass --zone ZONE_ID"
jq -e 'type == "array" and length > 0 and all(.[]; has("id") and has("name") and has("type"))' "$SNAP" >/dev/null || die "$SNAP is not a zone snapshot"
need CF_API_TOKEN_AGENT
protected='(.name | test("^_externaldns\\.")) or (.name | test("\\.dkim\\."))'
cur=$(mktemp); trap 'rm -f "$cur"' EXIT
page=1
: > "$cur"
while :; do
  resp=$(cf_api "$CF_API/zones/$ZONE/dns_records?per_page=5000&page=$page")
  echo "$resp" | jq -e '.success' >/dev/null || die "dns_records page $page failed: $(echo "$resp" | jq -c '.errors')"
  echo "$resp" | jq -c '.result[] | {id,name,type,content,data,priority,ttl,proxied,comment}' >> "$cur"
  pages=$(echo "$resp" | jq -r '.result_info.total_pages // 1')
  [ "$page" -ge "$pages" ] && break
  page=$((page + 1))
done
plan=$(jq -n --slurpfile snap "$SNAP" --slurpfile curx <(jq -s '.' "$cur") "
  (\$snap[0] | map(select(($protected) | not))) as \$s
  | (\$curx[0] | map(select(($protected) | not))) as \$c
  | (\$c | map({(.id): .}) | add // {}) as \$byid
  | (\$s | map({(.id): .}) | add // {}) as \$sbyid
  | {
      create: [\$s[] | select(\$byid[.id] == null)],
      update: [\$s[] | select(\$byid[.id] != null) | select(. != \$byid[.id])],
      delete: [\$c[] | select(\$sbyid[.id] == null)]
    }")
body() { jq -c 'del(.id) | (if .data != null then del(.content) | del(.priority) else del(.data) end) | with_entries(select(.value != null))' <<<"$1"; }
fail=0
for op in delete update create; do
  while IFS= read -r rec; do
    [ -n "$rec" ] || continue
    name=$(jq -r .name <<<"$rec"); type=$(jq -r .type <<<"$rec"); id=$(jq -r .id <<<"$rec")
    printf '%-6s %-5s %s\n' "$(echo "$op" | tr a-z A-Z)" "$type" "$name"
    [ "$APPLY" = 1 ] || continue
    case "$op" in
      create) r=$(cf_api -X POST "$CF_API/zones/$ZONE/dns_records" --data "$(body "$rec")") ;;
      update) r=$(cf_api -X PUT "$CF_API/zones/$ZONE/dns_records/$id" --data "$(body "$rec")") ;;
      delete) r=$(cf_api -X DELETE "$CF_API/zones/$ZONE/dns_records/$id") ;;
    esac
    jq -e '.success' <<<"$r" >/dev/null || { echo "  failed: $(jq -c '.errors' <<<"$r")"; fail=$((fail + 1)); }
  done < <(jq -c ".${op}[]" <<<"$plan")
done
printf 'mode=%s zone=%s create=%s update=%s delete=%s failed=%s\n' "$(mode)" "$ZONE" "$(jq '.create|length' <<<"$plan")" "$(jq '.update|length' <<<"$plan")" "$(jq '.delete|length' <<<"$plan")" "$fail"
[ "$fail" = 0 ]
