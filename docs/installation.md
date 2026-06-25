# Installation

This installation process involves a lot of building resources, then copying the resulting IDs back into configurations and building more resources. This is a rough guide, though it likely needs revision.

To install this project into a Kubernetes cluster, follow this checklist:

- Set up a NeonDB project as described in the next section, [NeonDB Setup](#neondb-setup).
- Build a `stalwart-server` IRSA role via the [platform-infrastructure Pulumi configurations](https://github.com/thunderbird/platform-infrastructure/blob/main/pulumi/environments/mzla-tb-dev/config.prod.yaml).
- Update the platform-infrastructure Pulumi config to allow DNS traffic to the EKS cluster security group, a la [this example in tb-dev](https://github.com/thunderbird/platform-infrastructure/blob/main/pulumi/environments/mzla-tb-dev/config.prod.yaml#L76-L89). `pulumi up` to apply this change.
- Create the secrets which need to be imported as `ExternalSecret` resources later. The secrets to create should be named as follows, but you will have to figure out the correct values for your installation:
    - `mzla/$env_name/stalwart-recovery-admin`: `{"recovery_admin": "username:password"}`
        - Sets the fallback [recovery administrator](https://stalw.art/docs/configuration/recovery-mode/#recovery-administrator) username and password
    - `mzla/$env_name/stalwart-postgresql-admin-credential`: `{"password": "admin_password_here"}`
        - Sets the password used by Stalwart to access its database.
    - `mzla/$env_name/cloudflare`: `{"apiToken": "cfut_something", "accountID": "0123456789abcdef"}`
        - Sets the Cloudflare operator's authentication details.
- Configure an appropriate set of Kustomize overlays for your use case. There will be some which you cannot fill out correctly. This is because some of these manifests generate AWS resources like security groups which others of these manifests depend upon. Use "empty" values in these cases, such as empty strings (`""`) or empty arrays (`[]`).
- Build an [ArgoCD `AppProject`](https://kubespec.dev/argo-cd/argoproj.io/v1alpha1/AppProject) allowing this repo to be deployed to your cluster. [Ref: thundermail in platform-infrastructure](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/projects/thundermail.yaml)
- For each installation, define an [ArgoCD `Application`](https://kubespec.dev/argo-cd/argoproj.io/v1alpha1/Application) with this repo as the source. Set the `path` option to the overlay directory corresponding to this installation. [Ref: thundermail in tb-dev](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/thundermail.yaml)
- Deploy via ArgoCD. Expect this sync to partially fail due to missing information.
- Locate the IDs of the resources which do get built. Make the following changes:
    - Add the stalwart-elasticache-redis security group ID to the `elasticache.yaml` overlay.
    - Add the stalwart-rds-postgresql security group ID to the `rds.yaml` overlay.
    - Set the eks-cluster-sg-mzla-eks-tb-prod01 security group ID on the `stalwart-server` `SecurityGroup` resource in `security-groups.yaml`.
    - Add ingress rules from the security group for the load balancer created by the `Service` resource to the `stalwart-server` security group.
    - Set the `stalwart-server` security group ID on the `stalwart-rds-postgresql` `SecurityGroup` resource in `security-groups.yaml`.

To find these IDs, either use the AWS CLI or web console to identify them, or issue `kubectl` commands like this one showing security group IDs:

```
# kubectl -n thundermail get securitygroup
NAME                         ID
stalwart-elasticache-redis   sg-abcdefg0123456789
stalwart-rds-postgresql      sg-abcdefg9876543210
stalwart-server              sg-gfedcba0123456789
```

- Deploy via ArgoCD again. This sync should again partially succeed. In particular, this should cause the RDS `DBInstance` resource to build. Wait for that to complete, then grab its connection endpoint.
- Update your environment's overlay for the `configmap.yaml` file so that the database config includes the connection endpoint as the `Host` field.
- Deploy via ArgoCD again.


