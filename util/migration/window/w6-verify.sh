#!/usr/bin/env bash
set -euo pipefail
W6_MODE=full
W6_PIECE=""
W6_RESUME=""
W6_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --compare-only|--folders-only) [ "$W6_MODE" = full ] || { echo "w6-verify.sh: use one of --compare-only, --folders-only, --piece" >&2; exit 64; }; W6_MODE=${1#--}; W6_MODE=${W6_MODE%-only} ;;
    --piece)
      [ "$W6_MODE" = full ] || { echo "w6-verify.sh: use one of --compare-only, --folders-only, --piece" >&2; exit 64; }
      case "${2:-}" in buckets|counts|bodies) ;; *) echo "w6-verify.sh: --piece takes buckets, counts or bodies" >&2; exit 64 ;; esac
      W6_MODE=piece; W6_PIECE=$2; shift ;;
    --resume) W6_RESUME=--resume ;;
    *) W6_ARGS+=("$1") ;;
  esac
  shift
done
[ -z "$W6_RESUME" ] || [ "$W6_MODE" = piece ] || { echo "w6-verify.sh: --resume needs --piece" >&2; exit 64; }
set -- "${W6_ARGS[@]+"${W6_ARGS[@]}"}"
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
case "$W6_MODE" in
  full)
    need STALWART_URL STALWART_USER STALWART_PASSWORD LEGACY_BLOB_BUCKET TARGET_BLOB_BUCKET KUBECONFIG_TARGET SECRET_DIR
    pending TARGET_NLB_DNS TEST_USER TEST_SENDER_APP_PASSWORD
    RUN_ARGS=() VSTEP=verify ;;
  compare)
    need STALWART_URL STALWART_USER STALWART_PASSWORD TARGET_BLOB_BUCKET KUBECONFIG_TARGET SECRET_DIR
    RUN_ARGS=(--group compare) VSTEP=verify-compare ;;
  folders)
    pending TARGET_NLB_DNS TEST_USER TEST_SENDER_APP_PASSWORD
    RUN_ARGS=() VSTEP="" ;;
  piece)
    need STALWART_URL STALWART_USER STALWART_PASSWORD LEGACY_BLOB_BUCKET TARGET_BLOB_BUCKET KUBECONFIG_TARGET SECRET_DIR
    RUN_ARGS=(--piece "$W6_PIECE" ${W6_RESUME:+"$W6_RESUME"}) VSTEP=verify-$W6_PIECE ;;
esac
VERIFY_OUT=$EVIDENCE_DIR/verify-$TS
VERIFY_PIECES_DIR=$EVIDENCE_DIR/verify-pieces
SOURCE_MISSING=""
SUITE_RAN=0
if [ "$TARGET_ENV" = tb-prod ] && { [ "$W6_MODE" = compare ] || [ "$W6_MODE" = full ] || { [ "$W6_MODE" = piece ] && [ "$W6_PIECE" != buckets ]; }; }; then
  for v in STALWART_SOURCE_PG_HOST STALWART_SOURCE_PG_DB STALWART_SOURCE_PG_USER STALWART_SOURCE_PG_PASSWORD; do [ -n "${!v:-}" ] || SOURCE_MISSING="$SOURCE_MISSING $v"; done
fi
EXPECTED_WARN=$MIG_DIR/verify/expected-warn.txt
UNEXPECTED_WARN=0
SUITE_FAILED=""
printf 'n/a: read-only step; %s stays as evidence\n' "$VERIFY_OUT" > "$RESTORE"

suite() {
  local out f left
  printf '  + . %s; bash %s > %s/run_all.txt 2>&1\n' "$MIG_DIR/verify/$TARGET_ENV.env" "$MIG_DIR/verify/run_all.sh${RUN_ARGS[*]:+ ${RUN_ARGS[*]}}" "$VERIFY_OUT"
  if [ -n "${SOURCE_MISSING:-}" ]; then
    [ "$APPLY" = 0 ] || { SUITE_FAILED="verify: empty in $ENV_FILE:${SOURCE_MISSING:-} (branch tb-prod-cutover-source; the reset phase of w4-migrate.sh writes them)"; echo "$SUITE_FAILED"; [ "$TARGET_ENV" != tb-prod ]; return; }
    printf 'PENDING  %s: the reset phase of w4-migrate.sh writes them; suite not run\n' "${SOURCE_MISSING# }"
    return 0
  fi
  [ "${W6_MODE:-full}" != piece ] || [ "$APPLY" = 1 ] || return 0
  SUITE_RAN=1
  mkdir -p "$VERIFY_OUT"
  out=$(VERIFY_OUT=$VERIFY_OUT VERIFY_PIECES_DIR=${VERIFY_PIECES_DIR:-} bash -c 'set -a; . "$1"; set +a; mkdir -p "$VERIFY_OUT"; bash "$2" "${@:3}" > "$VERIFY_OUT/run_all.txt" 2>&1; echo "exit=$? out=$VERIFY_OUT"' _ "$MIG_DIR/verify/$TARGET_ENV.env" "$MIG_DIR/verify/run_all.sh" "${RUN_ARGS[@]+"${RUN_ARGS[@]}"}") || return 1
  VERIFY_OUT=${out##* out=}
  f=$VERIFY_OUT/run_all.txt
  [ "${W6_MODE:-full}" = full ] || grep -E '^(== refusing|refusing|== piece [a-z]+: [0-9]+ batches|== piece [a-z]+: (not finished|stopping))' "$f" || true
  VALUE=$(( $(grep -c '^  FAIL  ' "$f" || true) + $(grep -c '^Traceback ' "$f" || true) ))
  UNEXPECTED_WARN=$(grep '^  WARN  ' "$f" | grep -vcFf <(grep -v '^[[:space:]]*$' "$EXPECTED_WARN") || true)
  printf 'run_all %s PASS=%s WARN=%s (not in expected-warn.txt: %s) FAIL=%s (FAIL lines plus %s crashed checks) (%s)\n' "${out%% *}" "$(grep -c '^  PASS  ' "$f" || true)" "$(grep -c '^  WARN  ' "$f" || true)" "$UNEXPECTED_WARN" "$VALUE" "$(grep -c '^Traceback ' "$f" || true)" "$f"
  echo "counts:"
  grep -E '^  (PASS|WARN)  .*[0-9]' "$f" | sed -E "s/'[^']*'/<x>/g; s/\"[^\"]*\"/<x>/g; s/[^ ]+@[^ ]+/<addr>/g" || true
  [ "$APPLY" = 0 ] || { [ "${out%% *}" = exit=0 ] && [ "$VALUE" = 0 ] && [ "$UNEXPECTED_WARN" = 0 ]; } || { SUITE_FAILED="verify: run_all ${out%% *} FAIL=$VALUE unexpected WARN=$UNEXPECTED_WARN ($f)"; echo "$SUITE_FAILED${VSTEP_NEXT:-; imap-jmap-folders runs next, then W6 stops}"; }
  [ -z "$SUITE_FAILED" ] || [ "$TARGET_ENV" != tb-prod ] || return 1
  [ "${W6_MODE:-full}" = piece ] || return 0
  left=$(sed -n "s/^== piece $W6_PIECE: [0-9]* batches; .*, left \([0-9]*\) (resume key .*/\1/p" "$f" | tail -1)
  [ "${left:-1}" = 0 ] && return 0
  printf 'NEED  piece %s: %s batch(es) left; rerun with --piece %s --resume in a new AWS session\n' "$W6_PIECE" "${left:-unknown}" "$W6_PIECE"
  NEEDS=$((NEEDS + 1))
}

imap_folders() {
  printf 'a LOGIN "%s" "%s"\r\na LIST "" "*"\r\nb LOGOUT\r\n' "$(cq "$TEST_USER")" "$(cq "$TEST_SENDER_APP_PASSWORD")" |
    timeout 20 openssl s_client -quiet -verify_return_error -verify_hostname "$MAIL_HOST" -connect "$1:993" -servername "$MAIL_HOST" 2>/dev/null | grep -c '^\* LIST' || true
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
if [ "${W6_MODE:-full}" = folders ]; then
  step imap-jmap-folders folders folders
  finish
  exit 0
fi
{ [ "${W6_MODE:-full}" = full ] && [ "$TARGET_ENV" != tb-prod ]; } || VSTEP_NEXT="; W6 stops"
helpers verify/run_all.sh verify/$TARGET_ENV.env verify/expected-warn.txt
if [ "${W6_MODE:-full}:${W6_PIECE:-}" = piece:buckets ]; then :; elif [ "$APPLY" = 1 ]; then port_forward || stop "no port-forward to Stalwart"; else port_forward || echo "no port-forward to Stalwart: target checks will fail"; fi
if live verify/$TARGET_ENV.env verify/run_all.sh verify/expected-warn.txt; then
  step "${VSTEP:-verify}" fails suite
  [ "${SUITE_RAN:-1}" = 0 ] || again "$(sn "${VSTEP:-verify}")" unexpected_warns "$UNEXPECTED_WARN" || stop "could not write the ${VSTEP:-verify} unexpected_warns row"
fi
[ "${W6_MODE:-full}" != full ] || step imap-jmap-folders folders folders
[ -z "$SUITE_FAILED" ] || stop "$SUITE_FAILED"
finish
