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

doh() { curl -s -m 20 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=A" | jq -r '[.Answer[]? | select(.type == 1) | .data][0] // empty'; }

imap() {
  IMAP_HOST=$TEST_MAILBOX_IMAP_HOST IMAP_USER=$TEST_MAILBOX_IMAP_USER IMAP_PASSWORD=$TEST_MAILBOX_IMAP_PASSWORD RCPT=$TEST_MAILBOX timeout $(( WAIT + 120 )) python3 -I - "$@" <<'PY'
import email, email.policy, email.utils, imaplib, os, re, ssl, sys, time
m = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], 993, ssl_context=ssl.create_default_context(), timeout=30)
m.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
if sys.argv[1] == "uidnext":
    print(m.status("INBOX", "(UIDNEXT)")[1][0].decode().split("UIDNEXT ")[1].rstrip(")"))
    sys.exit(0)
since, domain, deadline = int(sys.argv[2]), sys.argv[3], time.time() + int(sys.argv[4])
rcpt = os.environ["RCPT"].lower()
seen = set()
last = 4
while time.time() < deadline:
    m.select("INBOX", readonly=True)
    for uid in m.uid("search", None, f"UID {since}:*")[1][0].split():
        if int(uid) < since:
            continue
        raw = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE DKIM-SIGNATURE FEEDBACK-ID RETURN-PATH)])")[1][0][1]
        h = email.message_from_bytes(raw, policy=email.policy.default)
        subject = str(h.get("Subject", ""))
        addr = email.utils.parseaddr(str(h.get("From", "")))[1]
        to = [a.lower() for _, a in email.utils.getaddresses([str(v) for v in h.get_all("To", [])])]
        if uid in seen or rcpt not in to or not re.search(r"password|passwort", subject, re.I):
            continue
        seen.add(uid)
        dkim = sorted({d.lower() for v in h.get_all("DKIM-Signature", []) for d in re.findall(r"\bd=([^;\s]+)", str(v))})
        rp = email.utils.parseaddr(str(h.get("Return-Path", "")))[1].rpartition("@")[2].lower()
        ses = any(d == "amazonses.com" or d.endswith(".amazonses.com") for d in dkim + [rp])
        print(f"uid={uid.decode()} date={h.get('Date')} from={addr} subject={subject} dkim_d={','.join(dkim) or '-'} return_path_domain={rp or '-'} feedback_id={'present' if h.get('Feedback-ID') else 'absent'} ses={'yes' if ses else 'no'}")
        from_ok = addr.lower().endswith("@" + domain)
        if from_ok and ses:
            sys.exit(0)
        last = 5 if from_ok else 3
    time.sleep(15)
print("no reset mail from @" + domain + " via SES before the deadline")
sys.exit(last)
PY
}

ip=$(doh "$AUTH_HOST") || ip=
[ -n "$ip" ] || die "FAIL dns $AUTH_HOST has no A record"
RES="--resolve $AUTH_HOST:443:$ip"

code=$(curl -sS -m 30 $RES -c "$TMP/jar" -b "$TMP/jar" -L -o "$TMP/form.html" -w '%{http_code}' "$RESET_URL") || code="000 (curl rc $?)"
[ "$code" = 200 ] || die "FAIL reset-credentials GET HTTP $code"
action=$(grep -oE "(action=\"|formAction: ')[^\"']*login-actions/reset-credentials[^\"']*" "$TMP/form.html" | head -1 | sed -E "s/^(action=\"|formAction: ')//; s/&amp;/\&/g") || action=
[ -n "$action" ] || die "FAIL reset-credentials form has no action"
echo "PASS reset-credentials form on $AUTH_HOST"

uidnext=$(imap uidnext) || die "FAIL IMAP login to $TEST_MAILBOX_IMAP_HOST"
echo "PASS IMAP login $TEST_MAILBOX (UIDNEXT $uidnext)"

if [ "$APPLY" != 1 ]; then
  echo "dry-run: would POST username=$TEST_USER to the reset form, then poll $TEST_MAILBOX for ${WAIT}s for From @$FROM_DOMAIN relayed by SES"
  exit 0
fi

start=$(date -u +%FT%TZ)
code=$(curl -sS -m 30 $RES -c "$TMP/jar" -b "$TMP/jar" -o /dev/null -w '%{http_code}' --data-urlencode "username=$TEST_USER" "$action") || code="000 (curl rc $?)"
[[ "$code" =~ ^(200|302)$ ]] || die "FAIL reset-credentials POST HTTP $code; the reset may already have been sent, check $TEST_MAILBOX before rerunning"
echo "reset POST HTTP $code for $TEST_USER (Keycloak answers the same for unknown users; the mailbox poll decides)"

if out=$(imap poll "$uidnext" "$FROM_DOMAIN" "$WAIT"); then rc=0; else rc=$?; fi
end=$(date -u +%FT%TZ)
echo "$out"
[ "$rc" = 4 ] && echo "hint: check that TEST_USER's Keycloak email is TEST_MAILBOX before taking the SES fallback"
printf 'p3-smtp\t%s\t%s\tpoll_rc\t%s\n' "$start" "$end" "$rc" >> "${TIMING_TSV:-$EVIDENCE_DIR/timing-tb-stage.tsv}"
[ "$rc" = 0 ] && echo "P3 VERIFY: PASS" || { echo "P3 VERIFY: FAIL"; exit "$rc"; }
