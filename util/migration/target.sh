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
  local a prev="" seen=0 sub=${1:-}
  for a in "$@"; do
    [ "$a" != -- ] || break
    case $a in
      --context|--context=*|--cluster|--cluster=*|--server|--server=*|-s|-s?*|--token|--token=*|--user|--user=*|--username|--username=*|--password|--password=*|--as|--as=*|--as-group|--as-group=*|--as-uid|--as-uid=*|--client-certificate|--client-certificate=*|--client-key|--client-key=*|--certificate-authority|--certificate-authority=*|--insecure-skip-tls-verify|--insecure-skip-tls-verify=*|--tls-server-name|--tls-server-name=*|--proxy-url|--proxy-url=*)
        echo "refusing kubectl: $a picks a cluster or credential the --kubeconfig check did not see" >&2; return 64 ;;
    esac
    case $prev:$a in
      --kubeconfig:*|*:--kubeconfig=*)
        a=${a#--kubeconfig=}
        seen=1
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
  [ "$seen" = 1 ] || [ "$sub" = kustomize ] || { echo "refusing kubectl: no --kubeconfig, kubectl would use the current context" >&2; return 64; }
}
kubectl() { kube_args_ok "$@" || return 64; command kubectl "$@"; }
env_path_ok() {
  local f=$1 d
  d=$(dirname "$1")
  [ ! -L "$d" ] && [ -O "$d" ] && [ -z "$(find "$d" -maxdepth 0 -perm /022)" ] || return 1
  [ ! -e "$f" ] || { [ ! -L "$f" ] && [ -O "$f" ] && [ -z "$(find "$f" -maxdepth 0 -perm /022)" ]; }
}
env_target_ok() {
  local f
  env_path_ok "$1" || { echo "refusing $1: it or its directory is a symlink, not owned by $(id -un), or writable by group or others" >&2; return 64; }
  f=$(unset TARGET_ENV; set +eu; . "$1" >/dev/null 2>&1; printf '%s' "${TARGET_ENV:-}")
  [ "$f" = "$TARGET_ENV" ] || { [ -z "$f" ] && [ "$TARGET_ENV" = tb-stage ]; } || { echo "refusing $1: it was built for TARGET_ENV=${f:-<none>}, not $TARGET_ENV" >&2; return 64; }
}
operator_env_default() {
  local f=$HOME/.local/state/talos/cutover-operator-$TARGET_ENV.env
  [ "$TARGET_ENV" != tb-stage ] || [ -e "$f" ] || f=$HOME/.local/state/talos/cutover-operator.env
  printf '%s' "$f"
}
cutover_attempt_last() {
  local n
  n=$(find "$HOME/.local/state/talos/cutover-$TARGET_ENV" -mindepth 1 -maxdepth 1 -type d -name 'attempt-*' -printf '%f\n' 2>/dev/null | sed -n 's/^attempt-\([1-9][0-9]*\)$/\1/p' | sort -n | tail -1)
  printf '%s' "${n:-1}"
}
evidence_default() {
  if [ "$TARGET_ENV" = tb-prod ]; then printf '%s/attempt-%s' "$HOME/.local/state/talos/cutover-$TARGET_ENV" "${CUTOVER_ATTEMPT:-$(cutover_attempt_last)}"
  else printf '%s' "$HOME/.local/state/talos/cutover-$TARGET_ENV"; fi
}
secret_root() { printf '%s' "$HOME/.local/state/talos/cutover-$TARGET_ENV-secret"; }
secret_default() {
  if [ "$TARGET_ENV" = tb-prod ]; then printf '%s/attempt-%s' "$(secret_root)" "${CUTOVER_ATTEMPT:-$(cutover_attempt_last)}"
  else printf '%s' "/dev/shm/talos/cutover-$TARGET_ENV/secret-artifacts"; fi
}
secret_dir_ok() {
  [ -e "$1" ] || return 0
  [ ! -L "$1" ] && [ -d "$1" ] && [ -O "$1" ] && [ -z "$(find "$1" \( -perm /077 -o -type l -o ! -user "$(id -u)" \) -print -quit 2>/dev/null)" ]
}
env_lock_ready() {
  command -v flock >/dev/null 2>&1 || return 1
  [ -e "$1.lock" ] || ( umask 077; : >> "$1.lock" ) 2>/dev/null || return 1
  [ -f "$1.lock" ] && [ ! -L "$1.lock" ] && [ -r "$1.lock" ]
}
env_read() {
  if env_lock_ready "$1"; then
    { flock -s -w 60 9 || echo "the env file lock $1.lock stayed taken for 60 s: $1 read without it" >&2; cat "$1"; } 9< "$1.lock"
  else
    cat "$1"
  fi
}
load_cutover_env() {
  local want=$TARGET_ENV f
  ENV_FILE=${CUTOVER_ENV:-}
  if [ -z "$ENV_FILE" ]; then
    ENV_FILE=/dev/shm/talos/cutover-$TARGET_ENV.env
    for f in "$ENV_FILE" "$HOME/.local/state/talos/cutover-$TARGET_ENV.env"; do [ ! -f "$f" ] || { ENV_FILE=$f; break; }; done
  fi
  if [ -f "$ENV_FILE" ]; then
    if [ -L "$ENV_FILE" ] || [ -L "$(dirname "$ENV_FILE")" ] || [ ! -O "$ENV_FILE" ] || [ ! -O "$(dirname "$ENV_FILE")" ] || [ -n "$(find "$ENV_FILE" "$(dirname "$ENV_FILE")" -maxdepth 0 -perm /022)" ]; then
      echo "refusing to load $ENV_FILE: a symlink, not owned by $(id -un), or writable by group or others" >&2
      exit 1
    fi
    env_target_ok "$ENV_FILE" || exit 64
    set -a; eval "$(env_read "$ENV_FILE")"; set +a
    TARGET_ENV=$want
    [ "$TARGET_ENV" != tb-stage ] || : "${KUBECONFIG_TARGET:=${KUBECONFIG_STAGE:-}}"
  fi
  [ -z "${LANE_PF_PORT:-}" ] || STALWART_URL=http://localhost:$LANE_PF_PORT
  CUTOVER_ENV_LOADED=1
}
target_defines() { grep -q "^$1=" "$(dirname "${BASH_SOURCE[0]}")/targets/$TARGET_ENV.sh"; }
need_target() {
  local v
  for v in "$@"; do target_defines "$v" || { echo "$(basename "$0"): $v is not defined in targets/$TARGET_ENV.sh, so this script does not run for TARGET_ENV=$TARGET_ENV" >&2; exit 64; }; done
}
stage_only() {
  [ "${STAGE_ONLY_OK:-0}" = 1 ] && return 0
  echo "usage: $(basename "$0") is a tb-stage step; TARGET_ENV=$TARGET_ENV has no form of it" >&2
  exit 64
}
