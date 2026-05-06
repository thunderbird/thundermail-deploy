# thundermail-deploy

Manifests/configs for deploying Stalwart-as-Thundermail in Kubernetes.

ArgoCD pulls from this repo (currently from the `initial-setup` branch) and
applies into the `thundermail` namespace on EKS cluster `mzla-eks-tb-dev01`.
The `thundermail-app-of-apps` Application points at `argocd/` with
`directory.recurse: true`, so every YAML file under `argocd/` is applied — the
`kustomization.yaml` files in `argocd/aws/` and `argocd/stalwart/` are not
used by ArgoCD in directory mode.

## Layout

```
argocd/
  aws/bases/
    elasticache.yaml          # ACK ReplicationGroup, CacheSubnetGroup
    rds.yaml                  # ACK DBInstance, DBSubnetGroup
    security-groups.yaml      # ACK SecurityGroup + vpcresources SecurityGroupPolicy
  stalwart/bases/
    configmap.yaml            # Stalwart config
    secrets.yaml              # External Secrets pulling Stalwart creds
    deployment.yaml           # Stalwart Deployment (replaces the old StatefulSet)
    statefulset.yaml          # Currently fully commented out
    service.yaml              # ClusterIP for app/mgmt ports
    ingress.yaml              # nginx Ingress for the mail.* hostname
```

## Cluster context

| Thing                      | Value                                                |
|----------------------------|------------------------------------------------------|
| AWS account                | `718959508124` (mzla-tb-dev)                         |
| AWS region                 | `eu-central-1`                                       |
| EKS cluster                | `mzla-eks-tb-dev01`                                  |
| VPC                        | `vpc-0e0f4b8d603d463e0` (mzla-tb-dev-vpc)            |
| EKS auto-created cluster SG | `sg-08c51d43c4a250a81` (carried by node primary ENI) |
| Pulumi-managed cluster SG  | `sg-07624df6ceb8865ec` (additional SG, not the auto one) |
| Node instance type         | `m7g.large` (Graviton 3, supports SGP/branch ENIs)    |

These are referenced by ID in this repo because the ACK `SecurityGroup`
manifests are not aware of cluster-level resources managed elsewhere.

## How Security Groups for Pods (SGP) work here

Stalwart pods get a dedicated AWS security group attached to a *branch ENI* on
their assigned node. The mechanism is AWS's "Security Groups for Pods" feature,
implemented by the VPC CNI plus the in-cluster `vpcresources.k8s.aws/SecurityGroupPolicy`.

Flow:

1. The VPC CNI addon must have `ENABLE_POD_ENI=true` (set on the addon, currently true).
2. When a node registers, the EKS-side **VPC Resource Controller** attaches a *trunk* ENI
   to it. The node then advertises the resource `vpc.amazonaws.com/pod-eni` (capacity 9
   on `m7g.large`).
3. A `SecurityGroupPolicy` selects pods by label and lists SG IDs to attach.
4. The scheduler matches the pod's `vpc.amazonaws.com/pod-eni: 1` request against node
   capacity. The CNI then allocates a branch ENI carrying the listed SGs to that pod.

### Critical gotchas (learned the hard way)

**Trunk ENIs are only attached at node-register time.** If the VPC CNI's
`ENABLE_POD_ENI` flag is flipped *after* a node has already joined, that node
will never get a trunk and will never advertise `vpc.amazonaws.com/pod-eni`
capacity. Pods using `SecurityGroupPolicy` will be permanently unschedulable
with `Insufficient vpc.amazonaws.com/pod-eni`. The fix is to **recycle the
nodes** (e.g. `aws eks update-nodegroup-version --force` with no version
change does a rolling replace).

**The `vpc.amazonaws.com/has-trunk-attached` node label is cosmetic.** It
sometimes never appears even when a trunk *is* attached. The authoritative
check is the resource capacity: `kubectl get node ... -o
jsonpath='{.status.capacity.vpc\.amazonaws\.com/pod-eni}'`. If that's `9` (on
`m7g.large`), the trunk is attached and SGP will work.

**The pod's branch-ENI SG must allow kubelet probe traffic.** Liveness and
readiness probes originate from the node primary ENI, which carries the EKS
cluster SG (`sg-08c51d43c4a250a81`). If the SGP-attached SG does not allow
inbound probe ports from that SG, probes fail and the kubelet gracefully kills
the container. The container exits cleanly (reason: `Completed`, exitCode `0`)
because Stalwart honors SIGTERM, which is *not* the probe-failure signature
people are used to seeing. Stalwart's mgmt/probe port is `8080`.

**`DISABLE_TCP_EARLY_DEMUX` (init container env on aws-node)** should be
`true` if SGP pods use TCP-style probes on older kernels. With AL2023 / kernel
6.12 it's usually fine, but worth knowing if probes flap intermittently.

## How the ACK SecurityGroup needs to be wired

The `SecurityGroup` CRD from the EC2 ACK controller requires either `vpcID`
or `vpcRef`. We don't manage VPCs via ACK in this account, so use a literal
`vpcID: vpc-0e0f4b8d603d463e0`. Without it, ACK reports:

```
status.conditions[].reason: "resource reference wrapper or ID required: VPCID,VPCRef"
status.id: null
```

…and never creates the underlying AWS SG.

Once ACK creates the SG, it assigns a real SG ID. The
`SecurityGroupPolicy.spec.securityGroups.groupIds` list takes raw SG IDs (it
has no ref support — it's not an ACK CRD), so updating it is a manual
follow-up step after the ACK resource reconciles. Today this list contains
the Pulumi-managed cluster SG `sg-07624df6ceb8865ec` as a placeholder.

## Sync waves and ordering

Resources use `argocd.argoproj.io/sync-wave` to enforce ordering:

| Wave | Resource              |
|------|-----------------------|
| 1    | ACK SecurityGroup, RDS, ElastiCache primitives |
| 2    | Service               |
| 3    | Deployment (the workload) |
| 4    | Ingress               |

ArgoCD waits for all resources in a wave to be Healthy before moving on. If a
resource has a Health hook (Deployment, StatefulSet) and never becomes
Healthy, the whole sync stalls. Watch for **stuck syncs that block their own
fix**: e.g. an old StatefulSet whose pod can't pass probes blocks the very
sync wave that would have written the SG ingress rules to make probes pass.

## Pod selector / SGP gotchas

The `SecurityGroupPolicy` for Stalwart selects `app: stalwart`. The
Deployment/StatefulSet `template.metadata.labels.app` value must match
exactly — typos like `stalwarrt` mean:

- The Deployment is rejected by the API server (selector/template mismatch).
- Even if it weren't, pods wouldn't get the SGP-attached SG, wouldn't request
  `vpc.amazonaws.com/pod-eni`, and would silently lose their security
  boundary.

## Operational checklist when stalwart pods can't schedule or are crash-looping

1. `kubectl -n thundermail get pod stalwart-0 -o jsonpath='{.spec.containers[0].resources.requests}'`
   — does it request `vpc.amazonaws.com/pod-eni: 1`? If yes, SGP applies.
2. `kubectl get nodes -o json | jq '.items[].status.capacity."vpc.amazonaws.com/pod-eni"'`
   — at least one node should advertise `9`. If all `null`, no trunk ENIs are
   attached → recycle nodes.
3. `kubectl -n kube-system get ds aws-node -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ENABLE_POD_ENI")].value}'`
   — must be `"true"`.
4. `kubectl -n thundermail get securitygroup.ec2.services.k8s.aws stalwart-server -o jsonpath='{.status.id}'`
   — should be a real `sg-...` ID. If empty, look at
   `status.conditions[].reason` (most likely missing `vpcID`).
5. Check the SG attached to the pod's branch ENI allows tcp/8080 from
   `sg-08c51d43c4a250a81` (cluster SG).
6. If pods exit with `reason: Completed, exitCode: 0` after ~80s, that's the
   liveness-probe SIGTERM pattern (probe fails 3× over 30s, kubelet sends
   SIGTERM, Stalwart shuts down gracefully) — go fix #5.
