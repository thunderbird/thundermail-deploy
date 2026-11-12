#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need TARGET_NLB_DNS
pending TEST_USER
M=mail.stage-thundermail.com
OPERATOR_ENV=${OPERATOR_ENV:-$HOME/.local/state/talos/cutover-operator.env}
printf 'n/a: stores one value in the env file\n' > "$RESTORE"

capture() {
  local pw ip code imap src=stdin bad=""
  printf '  + read the app password created for %s in the Accounts UI from TEST_SENDER_APP_PASSWORD in %s, else stdin; check JMAP 443, IMAP LOGIN 993 and SMTP AUTH 587 through %s; append TEST_SENDER_APP_PASSWORD\n' "$TEST_USER" "$OPERATOR_ENV" "$TARGET_NLB_DNS"
  [ "$APPLY" = 1 ] || return 0
  pw=$(unset TEST_SENDER_APP_PASSWORD; load_operator_env "$OPERATOR_ENV"; printf '%s' "${TEST_SENDER_APP_PASSWORD:-}") || return 1
  if [ -n "$pw" ]; then
    src=$OPERATOR_ENV
  else
    [ -t 0 ] && printf 'app password for %s: ' "$TEST_USER" >&2
    IFS= read -rs pw || true
    [ -t 0 ] && printf '\n' >&2
  fi
  [ -n "$pw" ] || { echo "no TEST_SENDER_APP_PASSWORD in $OPERATOR_ENV and no password on stdin"; return 1; }
  ip=$(doh "$TARGET_NLB_DNS")
  [ -n "$ip" ] || { echo "no A record for $TARGET_NLB_DNS"; return 1; }
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -K <(curl_user "$TEST_USER" "$pw") --resolve "$M:443:$ip" "https://$M/jmap/session")
  [ "$code" = 200 ] || bad="$bad 443 (JMAP session $code)"
  imap=$(printf 'a LOGIN "%s" "%s"\r\nb LOGOUT\r\n' "$(cq "$TEST_USER")" "$(cq "$pw")" | timeout 20 openssl s_client -quiet -verify_return_error -verify_hostname "$M" -servername "$M" -connect "$ip:993" 2>/dev/null | grep -c '^a OK' || true)
  [ "$imap" = 1 ] || bad="$bad 993 (IMAP LOGIN)"
  SWAKS_OPT_auth_password="$pw" timeout 60 swaks --helo w7-check.local --server "$ip:587" --tls --tls-sni "$M" --tls-verify --tls-verify-target "$M" --auth PLAIN --auth-user "$TEST_USER" --from "$TEST_USER" --to "$TEST_USER" --quit-after AUTH >/dev/null 2>&1 || bad="$bad 587 (SMTP AUTH)"
  [ -z "$bad" ] || { echo "app password for $TEST_USER from $src failed on:$bad: password not stored"; return 1; }
  echo "app password for $TEST_USER from $src: JMAP 443, IMAP LOGIN 993 and SMTP AUTH 587 ok"
  VALUE=${#pw}
  setenv_secret TEST_SENDER_APP_PASSWORD "$pw"
}

step test-sender-password length capture
finish
