#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SNAP=${1:-}
[ -f "$SNAP" ] || die "usage: restore-replicas.sh replicas-before-<ts>.txt [--apply]"
grep -Eq '^[a-z0-9-]+ (Deployment|StatefulSet) [a-z0-9-]+ [0-9]+$' "$SNAP" || die "$SNAP has no replica lines"
grep -Ev '^$|^[a-z0-9-]+ (Deployment|StatefulSet) [a-z0-9-]+ [0-9]+$' "$SNAP" && die "$SNAP has malformed lines"
fail=0; n=0; scaled=()
while read -r ns kind name saved; do
  [ -n "$name" ] || continue
  current=$(kstage -n "$ns" get "$kind/$name" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo missing)
  [ "$APPLY" = 1 ] && [ "$current" = "$saved" ] && scaled+=("$ns $kind $name $saved")
  [ "$current" = "$saved" ] && continue
  n=$((n + 1))
  printf 'SCALE %s %s/%s %s -> %s\n' "$ns" "$kind" "$name" "$current" "$saved"
  [ "$APPLY" = 1 ] || continue
  kstage -n "$ns" scale "$kind/$name" --replicas="$saved" >/dev/null && scaled+=("$ns $kind $name $saved") || fail=$((fail + 1))
done < "$SNAP"
ready() { [ "$(kstage -n "$1" get "$2/$3" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" = "$4" ] || { [ "$4" = 0 ] && [ -z "$(kstage -n "$1" get "$2/$3" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" ]; }; }
if [ "$APPLY" = 1 ] && [ "${#scaled[@]}" -gt 0 ]; then
  deadline=$(( $(date +%s) + ${READY_TIMEOUT:-600} ))
  for w in "${scaled[@]}"; do
    set -- $w
    until ready "$@"; do
      [ "$(date +%s)" -lt "$deadline" ] || { echo "NOT READY $1 $2/$3: want $4 readyReplicas"; fail=$((fail + 1)); break; }
      sleep 10
    done
  done
fi
printf 'mode=%s workloads=%s to_scale=%s failed=%s\n' "$(mode)" "$(grep -c . "$SNAP")" "$n" "$fail"
[ "$fail" = 0 ]
