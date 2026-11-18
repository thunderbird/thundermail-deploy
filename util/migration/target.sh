case "${TARGET_ENV:-}" in
  tb-stage|tb-prod) ;;
  *)
    echo "usage: set TARGET_ENV to tb-stage or tb-prod (got '${TARGET_ENV:-}')" >&2
    case $- in *i*) return 64 ;; esac
    exit 64 ;;
esac
export TARGET_ENV
if [ "${TARGETS_SOURCED:-}" != "$TARGET_ENV" ]; then
  . "$(dirname "${BASH_SOURCE[0]}")/targets/$TARGET_ENV.sh"
  TARGETS_SOURCED=$TARGET_ENV
fi
[ "$TARGET_ENV" != tb-stage ] || : "${KUBECONFIG_TARGET:=${KUBECONFIG_STAGE:-}}"
PLACEHOLDERS=""
for _n in $(sed -n 's/^\(: "\${\)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)[:=].*/\2/p' "$(dirname "${BASH_SOURCE[0]}")/targets/$TARGET_ENV.sh"); do
  [[ ${!_n,,} != *replace_me* ]] || PLACEHOLDERS="$PLACEHOLDERS $_n"
done
for _n in $(grep -i 'replace_me' "$(dirname "${BASH_SOURCE[0]}")/verify/$TARGET_ENV.env" 2>/dev/null | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do PLACEHOLDERS="$PLACEHOLDERS verify/$TARGET_ENV.env:$_n"; done
unset _n
no_placeholders() {
  local v bad=""
  if [ $# = 0 ]; then bad=$PLACEHOLDERS; else for v in "$@"; do [[ ${!v:-} != *[Rr][Ee][Pp][Ll][Aa][Cc][Ee]_[Mm][Ee]* ]] || bad="$bad $v"; done; fi
  [ -z "$bad" ] || { echo "refusing: placeholder value in targets/$TARGET_ENV.sh or verify/$TARGET_ENV.env:$bad; fill it first" >&2; return 64; }
}
OTHER_CLUSTER=$(unset TARGET_CLUSTER; . "$(dirname "${BASH_SOURCE[0]}")/targets/$([ "$TARGET_ENV" = tb-prod ] && echo tb-stage || echo tb-prod).sh" >/dev/null 2>&1; printf '%s' "$TARGET_CLUSTER")
kube_cluster_of() {
  local cc
  cc=$(yq -r '.["current-context"] // ""' "$1" 2>/dev/null) && [ -n "$cc" ] || return 1
  CC=$cc yq -r '.contexts[] | select(.name == strenv(CC)) | .context.cluster' "$1" 2>/dev/null | head -1
}
kube_args_ok() {
  local a prev=""
  for a in "$@"; do
    [ "$a" != -- ] || break
    case $a in
      --context|--context=*|--cluster|--cluster=*|--server|--server=*|-s|-s?*|--token|--token=*|--user|--user=*)
        echo "refusing kubectl: $a picks a cluster or credential the --kubeconfig check did not see" >&2; return 64 ;;
    esac
    case $prev:$a in
      --kubeconfig:*|*:--kubeconfig=*)
        a=${a#--kubeconfig=}
        [ -n "$a" ] || { echo "refusing kubectl: --kubeconfig is empty, kubectl would use the current context" >&2; return 64; }
        [ -e "$a" ] || { echo "refusing kubectl: kubeconfig $a does not exist" >&2; return 64; }
        [ -r "$a" ] && [ ! -d "$a" ] || { echo "refusing kubectl: kubeconfig $a is a directory or not readable, so it cannot be checked" >&2; return 64; }
        ! grep -qF -- "$OTHER_CLUSTER" "$a" 2>/dev/null || { echo "refusing kubectl: kubeconfig $a names $OTHER_CLUSTER, not a $TARGET_ENV cluster" >&2; return 64; }
        [ "$TARGET_ENV" != tb-prod ] || [ "$a" != "${KUBECONFIG_TARGET:-}" ] || case $(kube_cluster_of "$a") in
          "$TARGET_CLUSTER"|*/"$TARGET_CLUSTER") ;;
          *) echo "refusing kubectl: the current-context of KUBECONFIG_TARGET $a is not cluster $TARGET_CLUSTER" >&2; return 64 ;;
        esac ;;
    esac
    prev=$a
  done
  [ "$prev" != --kubeconfig ] || { echo "refusing kubectl: --kubeconfig has no value" >&2; return 64; }
}
kubectl() { kube_args_ok "$@" || return 64; command kubectl "$@"; }
env_target_ok() {
  local f
  f=$(unset TARGET_ENV; set +eu; . "$1" >/dev/null 2>&1; printf '%s' "${TARGET_ENV:-}")
  [ "$f" = "$TARGET_ENV" ] || { [ -z "$f" ] && [ "$TARGET_ENV" = tb-stage ]; } || { echo "refusing $1: it was built for TARGET_ENV=${f:-<none>}, not $TARGET_ENV" >&2; return 64; }
}
operator_env_default() {
  local f=$HOME/.local/state/talos/cutover-operator-$TARGET_ENV.env
  [ "$TARGET_ENV" != tb-stage ] || [ -e "$f" ] || f=$HOME/.local/state/talos/cutover-operator.env
  printf '%s' "$f"
}
stage_only() {
  [ "${STAGE_ONLY_OK:-0}" = 1 ] && return 0
  echo "usage: $(basename "$0") is a tb-stage step; TARGET_ENV=$TARGET_ENV has no form of it" >&2
  exit 64
}
