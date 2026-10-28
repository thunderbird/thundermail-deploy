#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need ARGO_APPS KARGO_PROJECT_ACCOUNTS
suffix=before
[ "${1:-}" = post ] && suffix=before-thaw
gitops=$EVIDENCE_DIR/gitops-$suffix-$TS.yaml
replicas=$EVIDENCE_DIR/replicas-$suffix-$TS.txt
{
  khub -n argocd get application $ARGO_APPS -o yaml
  echo '---'
  khub -n "$KARGO_PROJECT_ACCOUNTS" get projectconfig "$KARGO_PROJECT_ACCOUNTS" -o yaml
} > "$gitops"
: > "$replicas"
for ns in thundermail accounts keycloak-customer; do
  kstage -n "$ns" get deployments,statefulsets -o jsonpath='{range .items[*]}{.metadata.namespace} {.kind} {.metadata.name} {.spec.replicas}{"\n"}{end}' >> "$replicas"
done
khub -n argocd get application $ARGO_APPS -o jsonpath='{range .items[*]}{.metadata.name} selfHeal={.spec.syncPolicy.automated.selfHeal}{"\n"}{end}'
khub -n "$KARGO_PROJECT_ACCOUNTS" get projectconfig "$KARGO_PROJECT_ACCOUNTS" -o jsonpath='{range .spec.promotionPolicies[*]}{.stage} autoPromotionEnabled={.autoPromotionEnabled}{"\n"}{end}'
cat "$replicas"
printf 'workloads=%s\n%s\n%s\n' "$(wc -l < "$replicas")" "$gitops" "$replicas"
