#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need TARGET_NLB_DNS
pending TEST_USER
M=mail.stage-thundermail.com
printf 'n/a: stores one value in the env file\n' > "$RESTORE"

capture() {
  local pw ip code
  printf '  + read the app password created for %s in the Accounts UI from stdin, check a JMAP session through %s, append TEST_SENDER_APP_PASSWORD\n' "$TEST_USER" "$TARGET_NLB_DNS"
  [ "$APPLY" = 1 ] || return 0
  [ -t 0 ] && printf 'app password for %s: ' "$TEST_USER" >&2
  IFS= read -rs pw || true
  [ -t 0 ] && printf '\n' >&2
  [ -n "$pw" ] || { echo "no password on stdin"; return 1; }
  ip=$(doh "$TARGET_NLB_DNS")
  [ -n "$ip" ] || { echo "no A record for $TARGET_NLB_DNS"; return 1; }
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -K <(curl_user "$TEST_USER" "$pw") --resolve "$M:443:$ip" "https://$M/jmap/session")
  [ "$code" = 200 ] || { echo "JMAP session for $TEST_USER returned $code: password not stored"; return 1; }
  VALUE=${#pw}
  setenv_secret TEST_SENDER_APP_PASSWORD "$pw"
}

step test-sender-password length capture
finish
