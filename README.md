# thundermail-deploy

Manifests for deploying Stalwart-as-Thundermail in Kubernetes.


## Components

### Kustomize

This repo is a [Kustomize](https://kustomize.io/) project. We rely upon the integration of Kustomize with both Kubernetes and [ArgoCD](https://argo-cd.readthedocs.io/en/stable/user-guide/kustomize/) to realize fully applicable Kubernetes manifests which build the resources we need for a deployment.


### ACK

This project makes use of [AWS Controllers for Kubernetes (ACK)](https://aws-controllers-k8s.github.io/community/docs/community/overview/). This allows us to define AWS resources that Stalwart depends on (like security groups or an ElastiCache instance) as Kubernetes manifests. Those manifests define custom Kubernetes resources which describe the properties of those AWS resources.

A [controller](https://github.com/aws-controllers-k8s) must be installed for each service category (EC2, RDS, etc.) in order for these manifests to become real resources. We install these via the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/). As a reference example, [here is the S3 controller installation](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/ack-s3-controller.yaml) for the tb-dev cluster.


### Cloudflare

We define certain resources on the Cloudflare platform in a very similar way that we use ACK to build AWS resources. We define Cloudflare resources as Kubernetes manifests. The [Cloudflare Kubernetes operator](https://github.com/adyanth/cloudflare-operator/tree/main) then reconciles custom Kubernetes resources into Cloudflare resources. The operator is installed via [platform-infrastructure](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/tb-dev-cloudflare-tunnel-operator.yaml).


### External Secrets

The `bases/stalwart/secrets.yaml` manifest converts secret data from its origin in [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) secrets into [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/) via the intermediary [External Secrets Operator](https://external-secrets.io/). That operator must be installed on the cluster for this project to work. The operator is installed via the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/external-secrets-operator.yaml).


### IAM Roles for Service Accounts (IRSA)

Stalwart needs to interact with other AWS services via API calls. In order to grant granular permission to services, we use Kubernetes `ServiceAccount`s bound to AWS IAM roles using the [IRSA model](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html).


### Per-Pod Security Groups

This project assumes that it is being installed on an AWS EKS cluster with [Security Groups Per Pod](https://docs.aws.amazon.com/eks/latest/best-practices/sgpp.html) enabled. We use a `SecurityGroupPolicy` to bind security groups to individual pods, and we grant access to those pod's service dependencies using those security groups. This prevents situations where, for example, one application has network/firewall permission to access another application's database.


## Repo Structure

- `kustomization.yaml`: Root configuration for the Kustomize configuration. Seeds the inventory for
    the base manifests.
- `bases`: Contains the base manifests to Kustomize. Key environment-specific information is omitted in these files.
    - `aws`: Contains the base manifests describing AWS ACK resources.
        - `elasticache.yaml`: Stalwart's Redis cache
        - `kustomization.yaml`: Kustomize inventory for AWS resources
        - `rds.yaml`: Stalwart's PostgreSQL instance
        - `s3.yaml`: Stalwart's blob storage bucket
        - `security-groups.yaml`: Security groups used by other resources, including per-pod security groups
        - `subnet-groups.yaml`: VPC subnet groups for use with managed data stores
    - `stalwart`: Contains the base manifests describing the Stalwart installation
        - `cloudflare-tunnel.yaml`: Inbound route via a Cloudflare tunnel bound to the Stalwart service
        - `configmap.yaml`: The minimal config file for Stalwart
        - `deployment.yaml`: The Stalwart deployment
        - `kustomization.yaml`: Kustomize inventory for Stalwart resources
        - `secrets.yaml`: Uses `ExternalSecret` resources to import secret data from AWS Secrets Manager
        - `serviceaccount.yaml`: Binds the Stalwart pod to an IAM role via IRSA
- `overlays`: Contains Kustomize patches for different installations. See "Overlay Structure" below.
- `util`: Contains offhand scripts useful to this repo


### Overlay Structure

In the `overlays` directory, you'll find more directories named after installations of this project. Each of these directories should contain YAML files with patches that environmentalize the install. These files follow the same organization as the `bases` directory, though this is a convention used to improve readability of the code and not a hard requirement. The `kustomization.yaml` file should list each of these files as patches.

Each overlay file should contain enough information to uniquely identify the resource it is patching. At minimum, you should include both the name of the resource and the namespace it lives in.


## Installation

To install this project into a Kubernetes cluster, follow this checklist:

- Build a `stalwart-server` IRSA role via the [platform-infrastructure Pulumi configurations](https://github.com/thunderbird/platform-infrastructure/blob/main/pulumi/environments/mzla-tb-dev/config.prod.yaml).
- Update the platform-infrastructure Pulumi config to allow DNS traffic to the EKS cluster security group, a la [this example in tb-dev](https://github.com/thunderbird/platform-infrastructure/blob/main/pulumi/environments/mzla-tb-dev/config.prod.yaml#L76-L89). `pulumi up` to apply this change.
- Configure an appropriate set of Kustomize overlays for your use case. There will be some which you cannot fill out correctly. This is because some of these manifests generate AWS resources like security groups which others of these manifests depend upon. Use "empty" values in these cases, such as empty strings (`""`) or empty arrays (`[]`).
- Build an [ArgoCD `AppProject`](https://kubespec.dev/argo-cd/argoproj.io/v1alpha1/AppProject) allowing this repo to be deployed to your cluster. [Ref: thundermail in platform-infrastructure](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/projects/thundermail.yaml)
- For each installation, define an [ArgoCD `Application`](https://kubespec.dev/argo-cd/argoproj.io/v1alpha1/Application) with this repo as the source. Set the `path` option to the overlay directory corresponding to this installation. [Ref: thundermail in tb-dev](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/thundermail.yaml)
- Deploy via ArgoCD. Expect this sync to partially fail due to missing information.
- Locate the IDs of the resources which do get built. Make the following changes:
    - Add the stalwart-elasticache-redis security group ID to the `elasticache.yaml` overlay.
    - Add the stalwart-rds-postgresql security group ID to the `rds.yaml` overlay.
    - Set the eks-cluster-sg-mzla-eks-tb-prod01 security group ID on the `stalwart-server` `SecurityGroup` resource in `security-groups.yaml`.
    - Set the `stalwart-server` security group ID on the `stalwart-rds-postgresql` `SecurityGroup` resource in `security-groups.yaml`.

To find these IDs, either use the AWS web console to identify them, or issue `kubectl` commands like this one showing security group IDs:

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


## Development and Debugging

### Kustomize

You can build these templates locally by [installing `kustomize`](https://kubectl.docs.kubernetes.io/installation/kustomize/) and running a build command. For example:

    kustomize build overlays/tb-dev

If successful, you should get a series of YAML manifests in the output. If not successful, you will receive a specific error message. These are the same error messages that would surface through ArgoCD if you were to merge and deploy the code, so this is really a requisite development step.


### ACK and Cloudflare Resources

ACK and Cloudflare resources both depend on the normal execution of Kubernetes operators which can reconcile the differences between the declared resources state in these manifests and the real state in the cloud. This extra component means there are two places you may have to check for debugging information when something goes wrong.

**First,** check the controller logs. You can do this with the ArgoCD web console (locate the "app-of-apps" for your cluster and you'll find the controller pods there). Alternately, with kubectl, first get the pod's full name:

    kubectl -n ack-system get pod

Then pull the logs for review:

    kubectl -n ack-system logs $POD_NAME

These logs reveal problems related to the controller's ability to work within AWS, such as authentication issues.

**Second,** you can look at events on the custom resources themselves, which reveal things like bad configurations and the results of `400 Bad Request` responses from the AWS API. For example, to investigate a security group, you might run:

    kubectl -n thundermail describe securitygroup stalwart-elasticache-redis


#### Service Linked Roles for the RDS ACK Controller

[Service Linked Roles for ACK documentation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAM.ServiceLinkedRoles.html) says this about the RDS controller's dependency upon its service linked role:

> You don't need to manually create a service-linked role. When you create a DB instance, Amazon RDS creates the service-linked role for you.
>
> ...
>
> If you delete this service-linked role, and then need to create it again, you can use the same process to recreate the role in your account. When you create a DB instance, Amazon RDS creates the service-linked role for you again.

Though one might expect this role to pop into existence when ACK tries to create a database, it does not. If the RDS ACK Controller logs show `400` errors from the API with a `Missing necessary credentials` error, it may be because the `AWSServiceRoleForRDS` IAM role has not been created, or has been deleted since it was initially created.

To resolve this, you can create any database at all in RDS (the fastest way is the quick setup wizard) and then delete it. RDS should automatically create the IAM role when you create the database.
