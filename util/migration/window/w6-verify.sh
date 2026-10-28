#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_URL STALWART_USER STALWART_PASSWORD TARGET_NLB_DNS LEGACY_BLOB_BUCKET TARGET_BLOB_BUCKET KUBECONFIG_STAGE SECRET_DIR
pending TEST_USER TEST_SENDER_APP_PASSWORD
MAIL_HOST=mail.stage-thundermail.com
VERIFY_OUT=$EVIDENCE_DIR/verify-$TS
printf 'n/a: read-only step; %s stays as evidence\n' "$VERIFY_OUT" > "$RESTORE"

suite() {
  local out
  printf '  + . %s; bash %s > %s/run_all.txt 2>&1\n' "$MIG_DIR/verify/stage.env" "$MIG_DIR/verify/run_all.sh" "$VERIFY_OUT"
  live verify/stage.env verify/run_all.sh || return "$APPLY"
  mkdir -p "$VERIFY_OUT"
  out=$(VERIFY_OUT=$VERIFY_OUT bash -c 'set -a; . "$1"; set +a; mkdir -p "$VERIFY_OUT"; bash "$2" > "$VERIFY_OUT/run_all.txt" 2>&1; echo "exit=$? out=$VERIFY_OUT"' _ "$MIG_DIR/verify/stage.env" "$MIG_DIR/verify/run_all.sh") || return 1
  VERIFY_OUT=${out##* out=}
  VALUE=$(grep -c '^  FAIL  ' "$VERIFY_OUT/run_all.txt" || true)
  printf 'run_all %s PASS=%s WARN=%s FAIL=%s (%s/run_all.txt)\n' "${out%% *}" "$(grep -c '^  PASS  ' "$VERIFY_OUT/run_all.txt" || true)" "$(grep -c '^  WARN  ' "$VERIFY_OUT/run_all.txt" || true)" "$VALUE" "$VERIFY_OUT"
  [ "$APPLY" = 0 ] || { [ "${out%% *}" = exit=0 ] && [ "$VALUE" = 0 ]; }
}

imap_folders() {
  printf 'a LOGIN "%s" "%s"\r\na LIST "" "*"\r\nb LOGOUT\r\n' "$(cq "$TEST_USER")" "$(cq "$TEST_SENDER_APP_PASSWORD")" |
    timeout 20 openssl s_client -quiet -connect "$1:993" -servername "$MAIL_HOST" 2>/dev/null | grep -c '^\* LIST' || true
}

jmap_folders() {
  local session api id
  session=$(curl -s -m 20 -K <(curl_user "$TEST_USER" "$TEST_SENDER_APP_PASSWORD") --resolve "$MAIL_HOST:443:$1" "https://$MAIL_HOST/jmap/session") || return 1
  id=$(jq -re '.primaryAccounts["urn:ietf:params:jmap:mail"]' <<<"$session") || return 1
  api=$(jq -r '.apiUrl' <<<"$session")
  curl -s -m 20 -K <(curl_user "$TEST_USER" "$TEST_SENDER_APP_PASSWORD") --resolve "$MAIL_HOST:443:$1" -H 'Content-Type: application/json' \
    --data "$(jq -cn --arg id "$id" '{using: ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"], methodCalls: [["Mailbox/get", {accountId: $id, properties: ["id"]}, "0"]]}')" "$api" |
    jq -r '.methodResponses[0][1].list | length'
}

folders() {
  local ip imap jmap
  printf '  + IMAP LOGIN %s with TEST_SENDER_APP_PASSWORD and LIST "" "*" on %s:993 (SNI %s); JMAP Mailbox/get through the same address\n' "$TEST_USER" "$TARGET_NLB_DNS" "$MAIL_HOST"
  [ "$APPLY" = 1 ] || return 0
  ip=$(doh "$TARGET_NLB_DNS")
  [ -n "$ip" ] || { echo "no A record for $TARGET_NLB_DNS via DoH"; return 1; }
  imap=$(imap_folders "$ip")
  jmap=$(jmap_folders "$ip") || { echo "JMAP session or Mailbox/get failed"; return 1; }
  echo "folders: IMAP=$imap JMAP=$jmap"
  VALUE=$imap
  [ "$imap" -gt 0 ] && [ "$imap" = "$jmap" ]
}

prereqs
helpers verify/run_all.sh verify/stage.env
if [ "$APPLY" = 1 ]; then port_forward || stop "no port-forward to Stalwart"; else port_forward || echo "no port-forward to Stalwart: target checks will fail"; fi
step verify fails suite
step imap-jmap-folders folders folders
finish
