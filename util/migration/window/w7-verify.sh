#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ -n "${CF_ZONE_ID:-}" ] || CF_ZONE_ID=$(shared_zone_id stage-thundermail.com || true)
need TARGET_NLB_DNS CF_ZONE_ID
pending TEST_USER TEST_MAILBOX TEST_SENDER TEST_SENDER_APP_PASSWORD
Z=stage-thundermail.com
M=mail.$Z
TABLE=$EVIDENCE_DIR/w7-verify-$(mode)-$TS.txt
SUBJECT=w7-verify-$TS
FAILS=0
printf 'n/a: read-only checks; the W7 restore file covers the records\n' > "$RESTORE"
: > "$TABLE"

dq() {
  curl -s -m 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=$2" |
    jq -r --arg t "$2" '.Answer[]? | select(.type == ({"A": 1, "CNAME": 5, "MX": 15, "TXT": 16, "SRV": 33, "TLSA": 52}[$t])) | .data' |
    sed 's/" "//g; s/^"//; s/"$//'
}
spf_of() { dq "$1" TXT | grep '^v=spf1' || true; }
one_spf() { local n; n=$(spf_of "$1" | grep -c . || true); [ "$n" = 1 ] || { echo "$1 has $n v=spf1 TXT"; return 1; }; [ -z "${2:-}" ] || [ "$(spf_of "$1")" = "$2" ] || { echo "$1 v=spf1 is '$(spf_of "$1")'"; return 1; }; echo "$1: one v=spf1${2:+, equal to the expected text}"; }

chain() {
  local name=$1 depth=$2 inc
  [ "$depth" -le 4 ] || { echo "include depth above 4 at $name"; return 1; }
  one_spf "$name" || return 1
  for inc in $(spf_of "$name" | tr ' ' '\n' | sed -n 's/^[+~?-]\{0,1\}include://p'); do chain "$inc" $((depth + 1)) || return 1; done
  [ "$depth" != 0 ] || echo "$name include chain: one v=spf1 at every hop"
}

chk() {
  local name=$1 out; shift
  if out=$("$@" 2>&1); then printf 'PASS  %-26s %s\n' "$name" "$(tail -1 <<<"$out" | cut -c1-100)" | tee -a "$TABLE"
  else FAILS=$((FAILS + 1)); printf 'FAIL  %-26s %s\n' "$name" "$(tail -1 <<<"$out" | cut -c1-100)" | tee -a "$TABLE"; fi
}

has() { local got; got=$(dq "$1" "$2"); grep -qiF -- "$3" <<<"$got" || { echo "$1 $2: ${got:-none}"; return 1; }; echo "$1 $2 has $3"; }
cname_to() { local got; got=$(dq "$1" CNAME | sed 's/\.$//'); [ "$got" = "$2" ] || { echo "$1 CNAME ${got:-none}"; return 1; }; echo "$1 -> $2"; }
any() { local n; n=$(dq "$1" "$2" | grep -c . || true); [ "$n" -gt 0 ] || { echo "$1 $2 missing"; return 1; }; echo "$1 $2 x$n"; }
jmap_443() {
  local got; got=$(dq "_jmap._tcp.$Z" SRV)
  if [ -z "$got" ] || awk '$3 != 443 {bad = 1} END {exit !bad}' <<<"$got"; then echo "_jmap._tcp: ${got:-none}"; return 1; fi
  echo "_jmap._tcp port 443 only"
}

spf_ttl() {
  local ttl doh
  [ -n "${CF_API_TOKEN_AGENT:-}" ] || { echo "CF_API_TOKEN_AGENT empty: TTL not read"; return 1; }
  ttl=$(cf_api "$CF_API/zones/$CF_ZONE_ID/dns_records?type=TXT&name=spf.$Z" | jq -r '[.result[] | select(.content | ltrimstr("\"") | startswith("v=spf1")) | .ttl] | join(",")')
  doh=$(curl -s -m 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=spf.$Z&type=TXT" | jq -r '[.Answer[]? | select(.type == 16) | .TTL] | max // empty')
  [ "$ttl" = 300 ] || { echo "spf.$Z ttl in Cloudflare is ${ttl:-none}, want 300"; return 1; }
  if [ -z "$doh" ] || [ "$doh" -gt 300 ]; then echo "spf.$Z DoH TTL ${doh:-none} above 300"; return 1; fi
  echo "spf.$Z ttl 300 (DoH $doh)"
}

dkim() {
  local names n=0 name
  [ -n "${CF_API_TOKEN_AGENT:-}" ] || { echo "CF_API_TOKEN_AGENT empty: selectors not listed"; return 1; }
  names=$(cf_api "$CF_API/zones/$CF_ZONE_ID/dns_records?type=TXT&per_page=5000" | jq -r --arg z "$Z" '.result[] | .name | select(endswith("._domainkey." + $z))' | sort -u)
  for name in $names; do has "$name" TXT 'v=DKIM1' >/dev/null || { echo "$name has no v=DKIM1"; return 1; }; n=$((n + 1)); done
  [ "$n" -gt 0 ] || { echo "no _domainkey TXT in the zone"; return 1; }
  echo "$n selector(s) resolve with v=DKIM1"
}

doh_before() { find "$EVIDENCE_DIR" -maxdepth 1 -name "zone-doh-before-$CF_ZONE_ID-*.txt" -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-; }

apex_same() {
  local before want
  before=$(doh_before)
  [ -s "$before" ] || { echo "no zone-doh-before file for the zone"; return 1; }
  want=$(awk -F'\t' -v z="$Z" '$1 == z && $2 == "TXT" {print $3}' "$before" | jq -r '.[] | gsub("\" \""; "") | ltrimstr("\"") | rtrimstr("\"") | select(startswith("v=spf1"))')
  [ "$(grep -c . <<<"$want")" = 1 ] || { echo "$(basename "$before") does not hold exactly one apex v=spf1"; return 1; }
  one_spf "$Z" "$want" || return 1
  echo "$Z v=spf1 unchanged from $(basename "$before")"
}

acme_same() {
  local before line name type ans
  before=$(doh_before)
  [ -s "$before" ] || { echo "no zone-doh-before file for the zone"; return 1; }
  while IFS=$'\t' read -r name type ans; do
    line=$(dq "$name" "$type" | sort | jq -Rsc 'split("\n") | map(select(length > 0))')
    [ "$line" = "$(jq -c 'map(ltrimstr("\"") | rtrimstr("\""))' <<<"$ans")" ] || { echo "$name changed"; return 1; }
  done < <(grep '^_acme-challenge\.' "$before")
  echo "_acme-challenge answers match $(basename "$before")"
}

well_known() {
  local ip p loc bad=""
  ip=$(doh "$Z")
  [ -n "$ip" ] || { echo "no A record for $Z"; return 1; }
  for p in caldav carddav jmap autoconfig; do
    loc=$(curl -s -m 15 -o /dev/null -w '%{redirect_url}' --resolve "$Z:443:$ip" "https://$Z/.well-known/$p")
    case "$loc" in https://mail.thundermail.com/*) bad="$bad $p" ;; esac
  done
  [ -z "$bad" ] || { echo "/.well-known/{${bad# }} on $Z still redirects to production mail.thundermail.com"; return 1; }
  echo "no /.well-known redirect from $Z to production"
}

tls() {
  local st=()
  [ -z "${2:-}" ] || st=(-starttls "$2")
  timeout 15 openssl s_client -connect "$NLB_IP:$1" -servername "$M" -verify_return_error -verify_hostname "$M" "${st[@]}" </dev/null >/dev/null 2>&1 || { echo "TLS on $NLB_IP:$1 failed"; return 1; }; echo "TLS $1 verified for $M"; }
mta_sts() { curl -s -m 15 --resolve "mta-sts.$Z:443:$NLB_IP" "https://mta-sts.$Z/.well-known/mta-sts.txt" | grep -q '^mode: testing' || { echo "policy is not mode: testing"; return 1; }; echo "mode: testing from the new NLB"; }

inbound() {
  timeout 60 swaks --to "$1" --from "$TEST_MAILBOX" --server "$NLB_IP:25" --tls --tls-sni "$M" --header "Subject: $SUBJECT" >/dev/null 2>&1 && { echo "250 for $1"; return 0; }
  timeout 10 bash -c "exec 3<>/dev/tcp/$NLB_IP/25" 2>/dev/null || { echo "NEED port 25 egress from this host: send to $1 from the TEST_MAILBOX provider"; return 0; }
  echo "swaks to $1 was refused"; return 1
}

arrived() {
  local s api id n
  s=$(curl -s -m 20 -K <(curl_user "$TEST_USER" "$TEST_SENDER_APP_PASSWORD") --resolve "$M:443:$NLB_IP" "https://$M/jmap/session") || return 1
  id=$(jq -re '.primaryAccounts["urn:ietf:params:jmap:mail"]' <<<"$s") || return 1
  api=$(jq -r .apiUrl <<<"$s")
  n=$(curl -s -m 20 -K <(curl_user "$TEST_USER" "$TEST_SENDER_APP_PASSWORD") --resolve "$M:443:$NLB_IP" -H 'Content-Type: application/json' \
    --data "$(jq -cn --arg id "$id" --arg s "$SUBJECT" '{using: ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"], methodCalls: [["Email/query", {accountId: $id, filter: {subject: $s}}, "0"]]}')" "$api" |
    jq -r '.methodResponses[0][1].ids | length')
  [ "${n:-0}" -gt 0 ]
}

delivery() {
  local start; start=$(date -u +%FT%TZ)
  until_ok 300 arrived || { echo "the inbound test message did not reach $TEST_USER over JMAP in 300s"; return 1; }
  row first-external-delivery "$start" "$(date -u +%FT%TZ)" subject "$SUBJECT"
  echo "delivered to $TEST_USER"
}

submission() {
  timeout 60 swaks --to "$TEST_MAILBOX" --from "$TEST_SENDER" --server "$NLB_IP:587" --tls --tls-sni "$M" --tls-verify --tls-verify-target "$M" --auth PLAIN --auth-user "$TEST_SENDER" --auth-password "$TEST_SENDER_APP_PASSWORD" --header "Subject: $SUBJECT-out" >/dev/null 2>&1 || { echo "authenticated submission failed"; return 1; }
  echo "250 for $TEST_MAILBOX; check SPF and DKIM pass in its received headers"
}

dns() {
  chk mx has "$Z" MX "10 $M."
  chk spf-apex apex_same
  chk spf-host one_spf "spf.$Z" "$SPF_DECIDED_TXT"
  chk spf-host-ttl spf_ttl
  chk spf-chain-apex chain "$Z" 0
  chk spf-chain-host chain "spf.$Z" 0
  chk dmarc has "_dmarc.$Z" TXT 'p=reject'
  chk mta-sts-txt has "_mta-sts.$Z" TXT 'v=STSv1'
  chk mta-sts-host any "mta-sts.$Z" CNAME
  chk autoconfig cname_to "autoconfig.$Z" "$M"
  chk autodiscover cname_to "autodiscover.$Z" "$M"
  chk ua-auto-config cname_to "ua-auto-config.$Z" "$M"
  chk ua-auto-config-txt any "_ua-auto-config.$Z" TXT
  chk tlsa any "_25._tcp.$M" TLSA
  chk jmap-srv jmap_443
  chk caldavs any "_caldavs._tcp.$Z" SRV
  chk carddavs any "_carddavs._tcp.$Z" SRV
  chk well-known well_known
  chk dkim dkim
  chk acme-unchanged acme_same
  chk mail-cname cname_to "$M" "$TARGET_NLB_DNS"
  VALUE=$FAILS
}

probes() {
  NLB_IP=$(doh "$TARGET_NLB_DNS")
  [ -n "$NLB_IP" ] || { echo "no A record for $TARGET_NLB_DNS"; return 1; }
  chk imaps tls 993
  chk smtps tls 465
  chk submission-starttls tls 587 smtp
  chk mta-sts-policy mta_sts
  VALUE=$FAILS
}

mail() {
  printf '  + swaks inbound to TEST_USER and postmaster@%s via %s:25 (subject %s), JMAP poll 300s, swaks 587 AUTH PLAIN as TEST_SENDER\n' "$Z" "$TARGET_NLB_DNS" "$SUBJECT"
  [ "$APPLY" = 1 ] || return 0
  chk inbound-test-user inbound "$TEST_USER"
  chk inbound-postmaster inbound "postmaster@$Z"
  chk first-delivery delivery
  chk submission submission
  NEEDS=$((NEEDS + $(grep -c 'NEED port 25' "$TABLE" || true)))
  VALUE=$FAILS
}

gate() { [ "$APPLY" = 0 ] || [ "$FAILS" = 0 ] || stop "$FAILS check(s) failed; table $TABLE"; }

prereqs
[ "$APPLY" = 0 ] || helpers swaks
step w7-dns fails dns
gate
step w7-probes fails probes
gate
step w7-mail fails mail
gate
printf 'table: %s (fails=%s)\n' "$TABLE" "$FAILS"
finish
