#!/usr/bin/env bash
set -euo pipefail
ALLOW_POS=1
case ${TARGET_ENV:-tb-prod} in tb-prod) ;; *) { echo "release-freight.sh looks up the release Freight of the tb-prod Stage; TARGET_ENV=${TARGET_ENV:-} has none" >&2; exit 64; } ;; esac
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ "${#POS[@]}" = 1 ] && [[ ${POS[0]} =~ ^[0-9a-f]{40}$ ]] || { echo "usage: release-freight.sh <40-hex thunderbird-accounts commit> [--dry-run | --apply] (prints the Freight name as the last line)" >&2; exit 64; }
need KARGO_PROJECT_ACCOUNTS KARGO_STAGE_ACCOUNTS KUBECONFIG_HUB
C=${POS[0]}
REPO=thunderbird/thunderbird-accounts
TBA=${TBA_DIR:-$HOME/src/tb/thunderbird-accounts}
ACCOUNTS_IMAGE=ghcr.io/thunderbird/thunderbird-accounts
KEYCLOAK_IMAGE=ghcr.io/thunderbird/thunderbird-accounts-keycloak
P=$KARGO_PROJECT_ACCOUNTS

resolve() {
  local image=$1 tags sha commit state digest
  tags=$(skopeo list-tags "docker://$image" | jq -r '.Tags[]') || { echo "skopeo list-tags $image failed"; return 1; }
  sha=$(grep -m1 -Fxf <(printf '%s\n' "$tags") <<<"$COMMITS") || { echo "no $image:<sha> tag on the first-parent history of $C"; return 1; }
  while read -r commit; do
    state=$(gh api "repos/$REPO/actions/workflows/publish-images.yml/runs?head_sha=$commit&event=push&branch=main" --jq '.workflow_runs | max_by(.created_at) | "\(.status)/\(.conclusion)"') || { echo "gh api publish-images runs for $commit failed"; return 1; }
    [ "$state" = completed/success ] || { echo "publish-images for $commit is $state: $image:$sha may not be the image of $C"; return 1; }
    [ "$commit" != "$sha" ] || break
  done <<<"$COMMITS"
  digest=$(skopeo inspect --format '{{.Digest}}' "docker://$image:$sha") || { echo "skopeo inspect $image:$sha failed"; return 1; }
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "bad digest for $image:$sha: $digest"; return 1; }
  echo "$image <- :$sha ($digest)"
  DIGEST=$digest
}

lookup() {
  local stage wh src freights n f
  COMMITS=$(git -C "$TBA" rev-list --first-parent "$C" 2>/dev/null) || { echo "$C is not a commit in $TBA (TBA_DIR): fetch origin there first"; return 1; }
  resolve "$ACCOUNTS_IMAGE" || return 1
  AD=$DIGEST
  resolve "$KEYCLOAK_IMAGE" || return 1
  KD=$DIGEST
  stage=$("${KH[@]}" -n "$P" get stage "$KARGO_STAGE_ACCOUNTS" -o json) || { echo "get stage $KARGO_STAGE_ACCOUNTS in $P failed"; return 1; }
  [ "$(jq '.spec.requestedFreight | length' <<<"$stage")" = 1 ] || { echo "Stage $KARGO_STAGE_ACCOUNTS requests Freight from $(jq '.spec.requestedFreight | length' <<<"$stage") origins, not 1"; return 1; }
  wh=$(jq -r '.spec.requestedFreight[0].origin | select(.kind == "Warehouse") | .name' <<<"$stage")
  src=$(jq -r '.spec.requestedFreight[0].sources | if .direct then "" else (.stages // [] | join(" ")) end' <<<"$stage")
  [ -n "$wh" ] || { echo "Stage $KARGO_STAGE_ACCOUNTS has no Warehouse origin"; return 1; }
  echo "Stage $KARGO_STAGE_ACCOUNTS: Warehouse $wh, verified in: ${src:-direct}"
  freights=$("${KH[@]}" -n "$P" get freight -o json | jq -c --arg wh "$wh" --arg a "$ACCOUNTS_IMAGE" --arg k "$KEYCLOAK_IMAGE" --arg ad "$AD" --arg kd "$KD" '[.items[]
    | select(.origin.kind == "Warehouse" and .origin.name == $wh)
    | select(([.images[]? | select(.repoURL == $a) | .digest][0] // "") == $ad and ([.images[]? | select(.repoURL == $k) | .digest][0] // "") == $kd)
    | {name: .metadata.name, alias: (.alias // .metadata.labels["kargo.akuity.io/alias"] // "-"), verified: ((.status.verifiedIn // {}) | keys), current: ((.status.currentlyIn // {}) | keys)}]') || { echo "get freight in $P failed"; return 1; }
  n=$(jq length <<<"$freights")
  [ "$n" != 0 ] || { echo "no Freight from Warehouse $wh carries accounts $AD and keycloak $KD: Kargo never saw this pair as :latest together, or it was deleted"; return 1; }
  [ "$n" = 1 ] || { echo "$n Freight from Warehouse $wh carry both digests: $(jq -r 'map(.name) | join(" ")' <<<"$freights")"; return 1; }
  f=$(jq -r '.[0].name' <<<"$freights")
  for s in $src; do
    jq -e --arg s "$s" '.[0].verified | index($s)' <<<"$freights" >/dev/null || { echo "Freight $f is not verified in $s, so Stage $KARGO_STAGE_ACCOUNTS cannot take it"; return 1; }
  done
  jq -r --arg ad "$AD" --arg kd "$KD" '.[0] | "Freight \(.name) (\(.alias)) accounts=\($ad) keycloak=\($kd) verified_in=\(.verified | join(",")) currently_in=\(.current | join(",") | if . == "" then "none" else . end)"' <<<"$freights"
  FOUND=$f
}

printf '== %s %s: read-only lookup of the Freight that carries thunderbird-accounts %s\n' "$W" "$(mode)" "$C"
printf '  + %s\n' "$(shq git -C "$TBA" rev-list --first-parent "$C")"
for image in "$ACCOUNTS_IMAGE" "$KEYCLOAK_IMAGE"; do
  printf '  + %s\n' "$(shq skopeo list-tags "docker://$image")"
  printf '  + %s   (each first-parent commit from %s down to the first one tagged in %s)\n' "$(shq gh api "repos/$REPO/actions/workflows/publish-images.yml/runs?head_sha=<commit>&event=push&branch=main" --jq '.workflow_runs | max_by(.created_at) | "\(.status)/\(.conclusion)"')" "$C" "$image"
  printf '  + %s\n' "$(shq skopeo inspect --format '{{.Digest}}' "docker://$image:<tagged commit>")"
done
printf '  + %s\n' "$(shq "${KH[@]}" -n "$P" get stage "$KARGO_STAGE_ACCOUNTS" -o json)"
printf '  + %s\n' "$(shq "${KH[@]}" -n "$P" get freight -o json)"
[ "$APPLY" = 1 ] || { finish; exit 0; }

FOUND=""
lookup || stop "no Freight to promote to $KARGO_STAGE_ACCOUNTS for $C"
printf '%s\n' "$FOUND"
