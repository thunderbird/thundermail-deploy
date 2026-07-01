# Installation

This installation process involves a lot of building resources, then copying the resulting IDs back into configurations and building more resources. This is a rough guide, though it likely needs revision. Overall, make whatever customizations you are able to make, commit and apply them, and see where the installation hangs up next. Repeat this until all the unknown values are known and part of the config.

Here is a checklist of known requirements to help:

- Set up a database, preferably a NeonDB Project as described in the next section, [NeonDB Setup](#neondb-setup).
- Build IRSA roles for pods that require IAM permissions to operate via the [platform-infrastructure Pulumi configurations](https://github.com/thunderbird/platform-infrastructure/blob/main/pulumi/environments/mzla-tb-dev/config.prod.yaml). Today, we need one for the `stalwart-server`.
- Create the secrets which need to be imported as `ExternalSecret` resources later. The secrets to create should be named as follows, but you will have to figure out the correct values for your installation:
    - `mzla/$env_name/stalwart-recovery-admin`: `{"recovery_admin": "username:password"}`
        - Sets the fallback [recovery administrator](https://stalw.art/docs/configuration/recovery-mode/#recovery-administrator) username and password
    - `mzla/$env_name/stalwart-postgresql-admin-credential`: `{"password": "admin_password_here"}`
        - Sets the password used by Stalwart to access its database.
    - `mzla/$env_name/stalwart-proxy-bearer-token`: `{"token": "your_very_long_password_here"}`
        - Sets the token the Stalwart proxy expects to see when you auth against its API.
- Request, validate, and export TLS certificates for use in Stalwart and Nginx. Instructions on doing this can be found in [the Configuration docs](./configuration.md#create-and-validate-an-ssl-certificate).
- Build an [ArgoCD `AppProject`](https://kubespec.dev/argo-cd/argoproj.io/v1alpha1/AppProject) allowing this repo to be deployed to your cluster. [Ref: thundermail in platform-infrastructure](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/projects/thundermail.yaml)
- For each installation, define an [ArgoCD `Application`](https://kubespec.dev/argo-cd/argoproj.io/v1alpha1/Application) with this repo as the source. Set the `path` option to the overlay directory corresponding to this installation. [Ref: thundermail in tb-dev](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/thundermail.yaml)
- Deploy via ArgoCD. Expect this sync to partially fail due to missing information.
- Configure an appropriate set of Kustomize overlays for your use case. There will be some which you cannot fill out correctly. This is because some of these manifests generate AWS resources like security groups which others of these manifests depend upon. Use "empty" values in these cases, such as empty strings (`""`) or empty arrays (`[]`). As you commit these changes and the various controllers build them and the resource IDs become known, come back and fill in those values. Commit those changes and push. Iterate in this fashion until the overlays are completely filled out.
- The EKS cluster security group must allow TCP and UDP traffic on port 53 from any security group used on a pod that needs to do local DNS lookups. That's `stalwart-server` and `stalwart-nginx` right now. This is also handled in the Pulumi code in platform-infrastructure.

To find the IDs of AWS resources built by ACK controllers, either use the AWS CLI or web console to identify them, or issue `kubectl` commands like this one showing security group IDs:

```
# kubectl -n thundermail get securitygroup
NAME                         ID
stalwart-elasticache-redis   sg-abcdefg0123456789
stalwart-server              sg-gfedcba0123456789
```
