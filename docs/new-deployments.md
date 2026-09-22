# How to Prepare a new Thundermail Deployment Environment

There are a series of dependencies that you have to step through in order to deploy this into a new environment. This document walks you through those steps.


## Answering two major questions

You'll need to know two primary things about your new deployment before you begin. So start by answering these questions to yourself:

1. Will I need to run this Stalwart installation behind a Stalwart Migration Proxy?
  - There are significant differences between the infrastructure of an installation that does and one that does not, and you will encounter multiple points in this process that depend on a consistent answer to this question.
2. Will this installation of Stalwart run in a split-delivery configuration where outbound email from some other mail server will have to be relayed over private network space to this installation?
  - Changes to this answer determine if we set up a special load balancer and security group rules to allow this relaying.


## Prepare supporting AWS resources

> [!NOTE]
> This section assumes some knowledge of the platform-infrastructure repo. Please familiarize yourself with at least its Pulumi components prior to running these steps.

Before you can run out any manifests to Kubernetes, you'll need to build a bunch of resources in AWS (like security groups, a Redis instance, etc.) first to support that deployment. This is done through a Pulumi module called [thundermail_deploy](https://github.com/thunderbird/platform-infrastructure/blob/main/pulumi/modules/thundermail_deploy.py). You'll need to set up an instance of this module prior to making any Kubernetes changes.

You'll need a call in your environment's `__main__.py` file to `thunderbird_deploy.create_application_dependencies`. If your target cluster already has a thundermail-deploy installation in it, this is probably already done. Double-check the code to make sure it's written in such a way that you can add a config section and have that picked up by the provisioning code. This configuration should be set off by a unique deployment name, which you'll want to reference elsewhere; make that decision now.

Then you'll need a configuration in your environment's `config.prod.yaml` file. The options are fully documented in the Pulumi module, but here is a stub configuration you can fill out:

```yaml
  deployment_name: # Unique deployment name that will be a part of every resource's name
    kubelet_probe_source_sgid: # SG of your cluster's control plane components
    s3_bucket_name: # Globally unique bucket name for blob storage
    namespace: # K8s namespace you're going to deploy into
    service_account: stalwart # There's no reason to change this value; this is hardcoded in a K8s manifest
    private_subnet_ids:
      - # Private subnet ID for first AZ
      - # Private subnet ID for second AZ
      - # Etc.
    admin_lb_sources: {} # SGs of other apps that need admin API access (Accounts, e.g.)
    services: ['all'] # Enable all supported services
    use_migration_proxy: # True/False, answer to one of the two Major Questions
    migration_proxy_sgid: # Only provide if use_migration_proxy is True; use SG of the migration proxy
    use_legacy_relay: # True/False, answer to one of the two Major Questions
    legacy_relay_sgids: [] # Legacy relay source SGs, like legacy-stage's node SGs; only provide if use_legacy_relay is True
    redis_name: # Name of your Redis instance
    redis_nodes: 1 # Increase for production envs
    redis_node_type: cache.t3.micro # Make this bigger for production envs
    role_name: tb-dev-tm-thundermail-stalwart # Name of the IAM role, which cannot exceed 40 characters
```

You can verify the resources slated to be built with a `pulumi preview`. When it's ready, open a PR, get it approved, and merge it. Post-merge actions on platform-infrastructure will run the `pulumi up` for you, creating the resources you need.

You will eventually need the details of various resources built by this operation, such as the IAM role ARN, the connection address of the Redis instance, security group IDs, etc. These will go into the Kustomize overlays for your deployment a little later down the line.


## Prepare SSL secrets

You'll need an SSL certificate that covers the domain Stalwart operates on. If you don't have one, follow our guides. You'll need to complete all of these steps:

- [Create a cert](./configuration.md#create-and-validate-an-ssl-certificate)
- [Export the cert](./configuration.md#export-and-format-the-ssl-certificate-for-stalwart-and-nginx)
- [Create a Kubernetes secret for nginx](./configuration.md#using-the-certificate-in-nginx)


## Create AWS Secrets Manager secrets

A pattern used here (and elsewhere) is to begin with handmade secret resources using AWS Secrets Manager, then the External Secrets Operator (ESO) will come along later and create Kubernetes Secret resources out of them. For this repo, you need two of these.


### Stalwart's recovery admin password

This credential is used to access the admin panel through our Tailscale mesh network. This can be any password you generate securely following best practices. You will also need to come up with a username. Combine these into basic auth format: `$USERNAME:$PASSWORD` to form the `$SECRET` in the following command:

```bash
aws --profile $AWS_PROFILE secretsmanager create-secret \
  --name "mzla/$ENVIRONMENT/$NAMESPACE/stalwart-recovery-admin" \
  --secret-string "{\"recovery_admin\": \"$SECRET\"}"
```


### Stalwart's database credentials

In the [NeonDB Console](https://console.neon.tech) (or however you prefer), either create a new database or a branch from one you want to point this Stalwart deployment at. Acquire the connection details. You'll need the host and username later, but you need the password now to create this secret:

```bash
aws --profile $AWS_PROFILE secretsmanager create-secret \
  --name "mzla/$ENVIRONMENT/$NAMESPACE/stalwart-postgresql-admin-credential" \
  --secret-string "{\"password\": \"$NEONDB_PASSWORD\"}"
```

If you have created a new database, or if the one you have branched is not configured in this way, be sure to restrict access to the database only from the right set of VPC endpoints. These IDs can be found with this command:

```bash
aws --profile $AWS_PROFILE ec2 describe-vpc-endpoints \
    --filter 'Name=tag:Name,Values=*-neondb-privatelink-*' \
    --query 'VpcEndpoints[*].VpcEndpointId' \
    --output text
```

If you pull your new Neon project's ID from its settings (this will something like `adjective-verb-abc123`), you can do the restriction by chaining that into a [NeonDB CLI](https://neon.com/docs/cli) command:

```bash
for vpce_id in $(
    aws --profile $AWS_PROFILE ec2 describe-vpc-endpoints \
        --filter 'Name=tag:Name,Values=*-neondb-privatelink-*' \
        --query 'VpcEndpoints[*].VpcEndpointId' \
        --output text
); do
    echo $vpce_id # Sometimes Neon's API errors without detail; you'll need to see which ones fail to retry them.
    neon vpc project restrict $vpce_id \
        --project-id $NEON_PROJECT_ID
done
```

You should generally also disable public access to your Neon project:

```bash
neon projects update $NEON_PROJECT_ID --block-public-connections
```

You will also need to make sure that the Stalwart pods have access to the PrivateLink security group for your environment. For example, in tb-dev, this SG is called `stalwart-neondb-privatelink`. Manually add both an inbound and an outbound rule permitting your Stalwart pod's SG to communicate over port 5432.


## Copy the Kustomize template

There's a Kustomize template ready for you to clone and fill out, so copy it into a new folder named after your deployment name from earlier.

```bash
cp -r overlays/{_template,$DEPLOYMENT_NAME}
```


## Update the Kustomize template

The template has several fields you will have to fill out. The previous Pulumi steps will have built out a series of resources whose IDs you will need to populate throughout your overlays. To ensure you fill everything out, the template you copied has the term `SETUP:` next to every field to populate or decision to make. All of these decisions are documented alongside the `SETUP:` term with instructions on how to adapt the overlay to your needs.

Run through all of these, (`grep -rn 'SETUP:' overlays/$DEPLOYMENT_NAME/` if you like) setting the right values where necessary, deleting the `SETUP:` term as you make each change.

Double-check that you have produced valid manifests by building your project:

```bash
kustomize build overlays/$DEPLOYMENT_NAME
```

Push your changes up. If you are not on a working branch, you'll need to go through with a merge to `main`. Allow ArgoCD to build the resources you've requested. Any problems you encounter at this point are beyond the scope of this documentation; you'll have to debug them as they arise.


## Create an ArgoCD application

Pull the latest commits in the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/) and branch it. Find an existing Argo Application manifest for this repo (such as the one at `argocd/tb-dev/apps/thundermail.yaml`) and copy it into the appropriate cluster application directory for your deployment.

- Make any adjustments to the relevant Project manifest to make sure your app is deployable to your chosen cluster and namespace.
- Update this Application manifest:
    - Make sure it has a unique app name.
    - Ensure the `project` reference is correct.
    - Point the Kustomize overlay `path` to the overlay you just created.
    - Ensure the destination server is accurate.
    - Update the destination `namespace` field to the one you decided on before.
    - If you like, you can set the source's `targetRevision` to a working branch while you do the initial buildout.

Create a PR with these changes. Get it reviewed and merged. You can either wait out the auto-sync period (<1 hour), or you can do the relevant refreshes (`argocd-projects` if you made changes to the project file, and the app-of-apps project for your deployment target). Since your overlay hasn't been updated and pushed, this should create your application, but that application should have deployment errors. We expect that right now.


## Exposing the admin panel to the Tailscale meshnet

In platform-infrastructure, you'll find a set of Ingress configurations for Tailscale:

- [tb-dev](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/tailscale/ingress.yaml)
- [tb-prod](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-prod/tailscale/ingress.yaml)

To expose your Stalwart web console, you'll need to make a new entry there. You can copy an existing Ingress and update its fields. Most importantly, change...

- ...`metadata.name` to something unique to this deployment,
- ...`metadata.namespace` to the namespace your deployment lives in, and
- ...`spec.tls.hosts.0` to same thing you set `metadata.name` to (or whatever you want the display name for this machine to be in the Tailscale console).

Once you've deployed this change, you can grab the Tailscale DNS address from the [machine details page](https://console.tailscale.com/admin/machines).


## Now what?

Now you will want to configure this installation to suit your needs. You can figure this out on your own, or you can refer to our [configuration docs](./configuration.md) to get started.

If you need to add this deployment as a destination behind a Stalwart Migration Proxy, see the [stalwart-migration-proxy-deploy readme](https://github.com/thunderbird/stalwart-migration-proxy-deploy/blob/main/README.md#adding-a-new-destination).
