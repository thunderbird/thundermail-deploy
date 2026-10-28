#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need KUBECONFIG_STAGE TIMING_TSV
printf 'n/a: read-only counts; the W7 restore file holds the rollback\n' > "$RESTORE"
START=$(after_cut dns-cutover | cut -f2)
FROM=dns-cutover
if [ -z "$START" ] || [ "$START" = unknown ]; then START=$(after_cut merge-148 | cut -f2); FROM=merge-148; fi

counts() {
  local logs started late
  if [ -z "$START" ]; then echo "no dns-cutover or merge-148 row for this cut: no DNS write has happened"; VALUE=0; QUEUED=0; return 0; fi
  echo "counting from the $FROM row: $START"
  started=$("${KS[@]}" -n thundermail get pods -l app=stalwart -o jsonpath='{range .items[*]}{.status.startTime}{"\n"}{end}') || return 1
  late=$(awk -v s="$START" '$1 > s' <<<"$started" | grep -c . || true)
  logs=$("${KS[@]}" -n thundermail logs -l app=stalwart --since-time="$START" --tail=-1 --max-log-requests=20) || return 1
  INBOUND=$(grep -cE '\(message-ingest\.(ham|spam)\)' <<<"$logs" || true)
  QUEUED=$(grep -cF '(queue.authenticated-message-queued)' <<<"$logs" || true)
  DELIVERED=$(grep -cF '(delivery.delivered)' <<<"$logs" || true)
  echo "since $START: inbound=$INBOUND (message-ingest.ham/spam) outbound_submitted=$QUEUED (queue.authenticated-message-queued) outbound_delivered=$DELIVERED (delivery.delivered)"
  [ "$late" = 0 ] || echo "INCOMPLETE: $late Stalwart pod(s) started after $START, so their earlier logs are gone and the counts are low"
  echo "inbound mail stays in the 0.16 store for re-injection; outbound mail already left and cannot be recalled"
  VALUE=$INBOUND
}

step inbound-since-flip messages counts
row "$(sn outbound-since-flip)" "$START" "$(date -u +%FT%TZ)" messages "$QUEUED"
finish
