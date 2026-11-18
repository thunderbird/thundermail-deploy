case "${TARGET_ENV:-}" in
  tb-stage|tb-prod) ;;
  *)
    echo "usage: set TARGET_ENV to tb-stage or tb-prod (got '${TARGET_ENV:-}')" >&2
    case $- in *i*) return 64 ;; esac
    exit 64 ;;
esac
export TARGET_ENV
. "$(dirname "${BASH_SOURCE[0]}")/targets/$TARGET_ENV.sh"
OTHER_CLUSTER=$(unset TARGET_CLUSTER; . "$(dirname "${BASH_SOURCE[0]}")/targets/$([ "$TARGET_ENV" = tb-prod ] && echo tb-stage || echo tb-prod).sh" >/dev/null 2>&1; printf '%s' "$TARGET_CLUSTER")
kubectl() {
  local a prev=""
  for a in "$@"; do
    case $prev:$a in
      --kubeconfig:*|*:--kubeconfig=*)
        a=${a#--kubeconfig=}
        [ -n "$a" ] || { echo "refusing kubectl: --kubeconfig is empty, kubectl would use the current context" >&2; return 64; }
        [ -e "$a" ] || { echo "refusing kubectl: kubeconfig $a does not exist" >&2; return 64; }
        ! grep -qF -- "$OTHER_CLUSTER" "$a" 2>/dev/null || { echo "refusing kubectl: kubeconfig $a names $OTHER_CLUSTER, not a $TARGET_ENV cluster" >&2; return 64; }
        [ "$TARGET_ENV" != tb-prod ] || [ "$a" != "${KUBECONFIG_TARGET:-}" ] || grep -qF -- "$TARGET_CLUSTER" "$a" 2>/dev/null || { echo "refusing kubectl: KUBECONFIG_TARGET $a does not name $TARGET_CLUSTER" >&2; return 64; } ;;
    esac
    prev=$a
  done
  [ "$prev" != --kubeconfig ] || { echo "refusing kubectl: --kubeconfig has no value" >&2; return 64; }
  command kubectl "$@"
}
env_target_ok() {
  local f
  f=$(unset TARGET_ENV; set +eu; . "$1" >/dev/null 2>&1; printf '%s' "${TARGET_ENV:-}")
  [ "$f" = "$TARGET_ENV" ] || { [ -z "$f" ] && [ "$TARGET_ENV" = tb-stage ]; } || { echo "refusing $1: it was built for TARGET_ENV=${f:-<none>}, not $TARGET_ENV" >&2; return 64; }
}
stage_only() {
  [ "${STAGE_ONLY_OK:-0}" = 1 ] && return 0
  echo "usage: $(basename "$0") is a tb-stage step; TARGET_ENV=$TARGET_ENV has no form of it" >&2
  exit 64
}
