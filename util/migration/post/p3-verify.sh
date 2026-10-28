#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
need TEST_USER TEST_MAILBOX TEST_MAILBOX_IMAP_HOST TEST_MAILBOX_IMAP_USER TEST_MAILBOX_IMAP_PASSWORD

AUTH_HOST=${AUTH_HOST:-auth.stage-thundermail.com}
FROM_DOMAIN=${FROM_DOMAIN:-auth-stage.tb.pro}
REALM=${REALM:-tbpro}
WAIT=${P3_MAIL_TIMEOUT:-300}
RESET_URL="https://$AUTH_HOST/realms/$REALM/login-actions/reset-credentials"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

doh() { curl -s -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=A" | jq -r '[.Answer[]? | select(.type == 1) | .data][0] // empty'; }

imap() {
  IMAP_HOST=$TEST_MAILBOX_IMAP_HOST IMAP_USER=$TEST_MAILBOX_IMAP_USER IMAP_PASSWORD=$TEST_MAILBOX_IMAP_PASSWORD python3 -I - "$@" <<'PY'
import email, email.utils, imaplib, os, sys, time
m = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], 993)
m.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
if sys.argv[1] == "uidnext":
    print(m.status("INBOX", "(UIDNEXT)")[1][0].decode().split("UIDNEXT ")[1].rstrip(")"))
    sys.exit(0)
since, domain, deadline = int(sys.argv[2]), sys.argv[3], time.time() + int(sys.argv[4])
while time.time() < deadline:
    m.select("INBOX", readonly=True)
    for uid in m.uid("search", None, f"UID {since}:*")[1][0].split():
        if int(uid) < since:
            continue
        raw = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")[1][0][1]
        h = email.message_from_bytes(raw)
        addr = email.utils.parseaddr(h.get("From", ""))[1]
        if "password" not in h.get("Subject", "").lower():
            continue
        print(f"uid={uid.decode()} date={h.get('Date')} from={addr} subject={h.get('Subject')}")
        sys.exit(0 if addr.lower().endswith("@" + domain) else 3)
    time.sleep(15)
print("no reset mail before the deadline")
sys.exit(4)
PY
}

ip=$(doh "$AUTH_HOST")
[ -n "$ip" ] || die "FAIL dns $AUTH_HOST has no A record"
RES="--resolve $AUTH_HOST:443:$ip"

code=$(curl -s $RES -c "$TMP/jar" -b "$TMP/jar" -L -o "$TMP/form.html" -w '%{http_code}' "$RESET_URL")
[ "$code" = 200 ] || die "FAIL reset-credentials GET HTTP $code"
action=$(grep -oE "(action=\"|formAction: ')[^\"']*login-actions/reset-credentials[^\"']*" "$TMP/form.html" | head -1 | sed -E "s/^(action=\"|formAction: ')//; s/&amp;/\&/g")
[ -n "$action" ] || die "FAIL reset-credentials form has no action"
echo "PASS reset-credentials form on $AUTH_HOST"

uidnext=$(imap uidnext) || die "FAIL IMAP login to $TEST_MAILBOX_IMAP_HOST"
echo "PASS IMAP login $TEST_MAILBOX (UIDNEXT $uidnext)"

if [ "$APPLY" != 1 ]; then
  echo "dry-run: would POST username=$TEST_USER to the reset form, then poll $TEST_MAILBOX for ${WAIT}s for From @$FROM_DOMAIN"
  exit 0
fi

start=$(date -u +%FT%TZ)
code=$(curl -s $RES -c "$TMP/jar" -b "$TMP/jar" -o /dev/null -w '%{http_code}' --data-urlencode "username=$TEST_USER" "$action")
[[ "$code" =~ ^(200|302)$ ]] || die "FAIL reset-credentials POST HTTP $code"
echo "PASS reset requested for $TEST_USER"

if out=$(imap poll "$uidnext" "$FROM_DOMAIN" "$WAIT"); then rc=0; else rc=$?; fi
end=$(date -u +%FT%TZ)
echo "$out"
printf 'p3-smtp\t%s\t%s\tfrom_domain_ok\t%s\n' "$start" "$end" "$([ "$rc" = 0 ] && echo 1 || echo 0)" >> "${TIMING_TSV:-$EVIDENCE_DIR/timing-tb-stage.tsv}"
[ "$rc" = 0 ] && echo "P3 VERIFY: PASS" || { echo "P3 VERIFY: FAIL"; exit "$rc"; }
