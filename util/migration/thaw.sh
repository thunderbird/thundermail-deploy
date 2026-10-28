#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need FREEZE_PR PI_DIR
ENV_FILE=${CUTOVER_ENV:-${ENV_FILE:-/dev/shm/talos/cutover-tb-stage.env}}
PI_REPO=thunderbird/platform-infrastructure
run() { printf '  + %s\n' "$*"; [ "$APPLY" = 1 ] || return 0; "$@"; }

IFS=$'\t' read -r STATE FREEZE_SHA < <(gh pr view "$FREEZE_PR" -R "$PI_REPO" --json state,mergeCommit -q '[.state, (.mergeCommit.oid // "")] | @tsv')
[ "$STATE" = MERGED ] && [ -n "$FREEZE_SHA" ] || die "freeze PR $FREEZE_PR is ${STATE:-unknown} with no merge commit: nothing to thaw"
echo "mode=$(mode) FREEZE_SHA=$FREEZE_SHA"

git -C "$PI_DIR" fetch origin main
if git -C "$PI_DIR" fetch origin thaw-tb-stage 2>/dev/null; then
  grep -q "This reverts commit $FREEZE_SHA" <<<"$(git -C "$PI_DIR" log origin/main..origin/thaw-tb-stage --format=%B)" || die "origin/thaw-tb-stage does not revert $FREEZE_SHA"
  run git -C "$PI_DIR" checkout -B thaw-tb-stage origin/thaw-tb-stage
else
  run git -C "$PI_DIR" checkout -B thaw-tb-stage origin/main
  run git -C "$PI_DIR" revert --no-edit -m 1 "$FREEZE_SHA"
  run git -C "$PI_DIR" push -u origin thaw-tb-stage
fi

BODY="Reverts $FREEZE_SHA (freeze PR https://github.com/$PI_REPO/pull/$FREEZE_PR): tb-stage selfHeal and Kargo auto-promotion back on.

Manual steps
- Merge at https://github.com/$PI_REPO/issues/1572 with Create a merge commit. Squash or rebase changes the SHA and makes \`git revert -m 1\` fail.

Done when
- The four Applications show selfHeal true and the tb-accounts ProjectConfig shows tb-stage true on the hub.

Part of https://github.com/$PI_REPO/issues/1536
Refs https://github.com/$PI_REPO/issues/1538"

if [ "$APPLY" != 1 ]; then
  printf '  + gh pr create -R %s --draft --base main --head thaw-tb-stage --title "thaw: tb-stage selfHeal + auto-promotion" (body %s chars)\n' "$PI_REPO" "${#BODY}"
  printf '  + append FREEZE_SHA=%s and THAW_PR to %s\n' "$FREEZE_SHA" "$ENV_FILE"
  echo "mode=dry-run nothing written: rerun with --apply"
  exit 0
fi
THAW_PR=$(gh pr create -R "$PI_REPO" --draft --base main --head thaw-tb-stage \
  --title "thaw: tb-stage selfHeal + auto-promotion" --body "$BODY" 2>/dev/null \
  || gh pr view thaw-tb-stage -R "$PI_REPO" --json url,state -q 'select(.state == "OPEN") | .url')
: "${THAW_PR:?no open thaw PR for thaw-tb-stage}"

printf 'FREEZE_SHA=%s\nTHAW_PR=%s\n' "$FREEZE_SHA" "$THAW_PR" >> "$ENV_FILE"
echo "FREEZE_SHA=$FREEZE_SHA"
echo "THAW_PR=$THAW_PR"
