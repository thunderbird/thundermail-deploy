#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SNAP=${1:-}
[ -f "$SNAP" ] || die "usage: restore-replicas.sh replicas-before-<ts>.txt [--apply]"
fail=0; n=0
while read -r ns kind name saved; do
  [ -n "$name" ] || continue
  current=$(kstage -n "$ns" get "$kind/$name" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo missing)
  [ "$current" = "$saved" ] && continue
  n=$((n + 1))
  printf 'SCALE %s %s/%s %s -> %s\n' "$ns" "$kind" "$name" "$current" "$saved"
  [ "$APPLY" = 1 ] || continue
  kstage -n "$ns" scale "$kind/$name" --replicas="$saved" >/dev/null || fail=$((fail + 1))
done < "$SNAP"
printf 'mode=%s workloads=%s to_scale=%s failed=%s\n' "$(mode)" "$(grep -c . "$SNAP")" "$n" "$fail"
[ "$fail" = 0 ]
