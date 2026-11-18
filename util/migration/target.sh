case "${TARGET_ENV:-}" in
  tb-stage|tb-prod) ;;
  *)
    echo "usage: set TARGET_ENV to tb-stage or tb-prod (got '${TARGET_ENV:-}')" >&2
    case $- in *i*) return 64 ;; esac
    exit 64 ;;
esac
export TARGET_ENV
. "$(dirname "${BASH_SOURCE[0]}")/targets/$TARGET_ENV.sh"
kubectl() {
  local a prev=""
  for a in "$@"; do
    case $prev:$a in
      --kubeconfig:*|*:--kubeconfig=*)
        a=${a#--kubeconfig=}
        [ -n "$a" ] || { echo "refusing kubectl: --kubeconfig is empty, kubectl would use the current context" >&2; return 64; }
        [ -e "$a" ] || { echo "refusing kubectl: kubeconfig $a does not exist" >&2; return 64; } ;;
    esac
    prev=$a
  done
  [ "$prev" != --kubeconfig ] || { echo "refusing kubectl: --kubeconfig has no value" >&2; return 64; }
  command kubectl "$@"
}
stage_only() {
  [ "${STAGE_ONLY_OK:-0}" = 1 ] && return 0
  echo "usage: $(basename "$0") is a tb-stage step; TARGET_ENV=$TARGET_ENV has no form of it" >&2
  exit 64
}
