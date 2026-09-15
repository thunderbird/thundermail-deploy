# Deployment Platform Components


Thundermail is deployed on Kubernetes clusters on the AWS EKS platform. As such, it depends on a number of related technologies and configurations.


## Kubernetes Components


### Kustomize

This repo is a [Kustomize](https://kustomize.io/) project. We rely upon the integration of Kustomize with both Kubernetes and [ArgoCD](https://argo-cd.readthedocs.io/en/stable/user-guide/kustomize/) to realize fully applicable Kubernetes manifests which build the resources we need for a deployment.


### AWS Load Balancer Controller

We use this controller to allow load balanced traffic to our Stalwart pods from outside the network. The controller responds to various conditions in an EKS cluster to create AWS load balancers. In particular, we use annotations on our `Service` resources that trigger the creation of load balancers.

In the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/aws-lb-controller.yaml), we install this into the relevant repos using the [Helm installation method](https://docs.aws.amazon.com/eks/latest/userguide/lbc-helm.html). For example, we [annotate a service](../overlays/tb-dev/stalwart-mail/service.yaml) to build a load balancer (with listeners and target groups) allowing traffic to our mail services. Valid annotations and the rest of the product's features are [documented here](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/service/annotations/).


### External Secrets

The `bases/stalwart/secrets.yaml` manifest converts secret data from its origin in [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) secrets into [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/) via the intermediary [External Secrets Operator](https://external-secrets.io/). That operator must be installed on the cluster for this project to work. The operator is installed via the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/external-secrets-operator.yaml). Those secrets are created manually prior to deploying these manifests.


## EKS Components

### IAM Roles for Service Accounts (IRSA)

Stalwart needs to interact with other AWS services via API calls. In order to grant granular permission to services, we use Kubernetes `ServiceAccount`s bound to AWS IAM roles using the [IRSA model](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html).


### Per-Pod Security Groups

This project assumes that it is being installed on an AWS EKS cluster with [Security Groups Per Pod](https://docs.aws.amazon.com/eks/latest/best-practices/sgpp.html) enabled. We use a `SecurityGroupPolicy` to bind security groups to individual pods, and we grant access to those pod's service dependencies using those security groups. This prevents situations where, for example, one application has network/firewall permission to access another application's database.


## Supporting Resources

When deploying an instance of this repo, you will need to build some supporting resources. These are constructed in our [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/) through the [thundermail_deploy module](https://github.com/thunderbird/platform-infrastructure/). Full details on how to deploy this repo can be found in the [New Deployments docs](./new-deployments.md).
