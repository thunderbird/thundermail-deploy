#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_target THAW_BRANCH THAW_MERGE_ISSUE THAW_PART_OF_ISSUE THAW_REFS_ISSUE
need FREEZE_PR PI_DIR
no_placeholders THAW_BRANCH THAW_MERGE_ISSUE THAW_PART_OF_ISSUE THAW_REFS_ISSUE KARGO_PROJECT_ACCOUNTS KARGO_STAGE_ACCOUNTS || exit 64
ENV_FILE=${CUTOVER_ENV:-${ENV_FILE:-/dev/shm/talos/cutover-$TARGET_ENV.env}}
[ ! -f "$ENV_FILE" ] || env_target_ok "$ENV_FILE" || exit 64
PI_REPO=thunderbird/platform-infrastructure
run() { printf '  + %s\n' "$*"; [ "$APPLY" = 1 ] || return 0; "$@"; }

IFS=$'\t' read -r STATE FREEZE_SHA < <(gh pr view "$FREEZE_PR" -R "$PI_REPO" --json state,mergeCommit -q '[.state, (.mergeCommit.oid // "")] | @tsv')
[ "$STATE" = MERGED ] && [ -n "$FREEZE_SHA" ] || die "freeze PR $FREEZE_PR is ${STATE:-unknown} with no merge commit: nothing to thaw"
echo "mode=$(mode) FREEZE_SHA=$FREEZE_SHA"

git -C "$PI_DIR" fetch origin main
if git -C "$PI_DIR" fetch origin "$THAW_BRANCH" 2>/dev/null; then
  grep -q "This reverts commit $FREEZE_SHA" <<<"$(git -C "$PI_DIR" log "origin/main..origin/$THAW_BRANCH" --format=%B)" || die "origin/$THAW_BRANCH does not revert $FREEZE_SHA"
  run git -C "$PI_DIR" checkout -B "$THAW_BRANCH" "origin/$THAW_BRANCH"
else
  run git -C "$PI_DIR" checkout -B "$THAW_BRANCH" origin/main
  run git -C "$PI_DIR" revert --no-edit -m 1 "$FREEZE_SHA"
  run git -C "$PI_DIR" push -u origin "$THAW_BRANCH"
fi

BODY="Reverts $FREEZE_SHA (freeze PR https://github.com/$PI_REPO/pull/$FREEZE_PR): $TARGET_ENV selfHeal and Kargo auto-promotion back on.

Manual steps
- Merge at https://github.com/$PI_REPO/issues/$THAW_MERGE_ISSUE with Create a merge commit. Squash or rebase changes the SHA and makes \`git revert -m 1\` fail.

Done when
- The four Applications show selfHeal true and the $KARGO_PROJECT_ACCOUNTS ProjectConfig shows $KARGO_STAGE_ACCOUNTS true on the hub.

Part of https://github.com/$PI_REPO/issues/$THAW_PART_OF_ISSUE
Refs https://github.com/$PI_REPO/issues/$THAW_REFS_ISSUE"

if [ "$APPLY" != 1 ]; then
  printf '  + gh pr create -R %s --draft --base main --head %s --title "thaw: %s selfHeal + auto-promotion" (body %s chars)\n' "$PI_REPO" "$THAW_BRANCH" "$TARGET_ENV" "${#BODY}"
  printf '  + append FREEZE_SHA=%s and THAW_PR to %s\n' "$FREEZE_SHA" "$ENV_FILE"
  echo "mode=dry-run nothing written: rerun with --apply"
  exit 0
fi
THAW_PR=$(gh pr create -R "$PI_REPO" --draft --base main --head "$THAW_BRANCH" \
  --title "thaw: $TARGET_ENV selfHeal + auto-promotion" --body "$BODY" 2>/dev/null \
  || gh pr view "$THAW_BRANCH" -R "$PI_REPO" --json url,state -q 'select(.state == "OPEN") | .url')
: "${THAW_PR:?no open thaw PR for $THAW_BRANCH}"

env_path_ok "$ENV_FILE" || die "refusing to append to $ENV_FILE: a symlink, not owned by $(id -un), or writable by group or others"
printf 'FREEZE_SHA=%s\nTHAW_PR=%s\n' "$FREEZE_SHA" "$THAW_PR" >> "$ENV_FILE"
echo "FREEZE_SHA=$FREEZE_SHA"
echo "THAW_PR=$THAW_PR"
