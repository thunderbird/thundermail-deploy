# How to Prepare a new Thundermail Deployment Environment

There are a series of dependencies that you have to step through in order to deploy this into a new environment. This document walks you through those steps.


## Prepare SSL secrets

You'll need an SSL certificate that covers the domain Stalwart operates on. If you don't have one, follow our guides. You'll need to complete all of these steps:

- [Create a cert](./configuration.md#create-and-validate-an-ssl-certificate)
- [Export the cert](./configuration.md#export-and-format-the-ssl-certificate-for-stalwart-and-nginx)
- [Create a Kubernetes secret for nginx](./configuration.md#using-the-certificate-in-nginx)


## Prepare an IAM role

Stalwart will eventually store data in an S3 bucket. You don't want your deployment to be able to manipulate other deployments' bucket content, and vice versa, so you'll have to create a new role targeting your deployment's bucket (which does not exist yet, but you'll have to come up with the name for it now).

In the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/), open the Pulumi config file for your target environment (`pulumi/environments/$ENV/config.prod.yaml`) and locate the `irsa.roles` section.

Add an entry to this mapping indicating your deployment:

```yaml
    stalwart-deploymentname:
      namespace: your_deployment_namespace
      service_account: stalwart # Leave this as-is unless you change it in Kustomize
      policies:
        - effect: Allow
          actions:
            - "s3:*"
          resources:
            - "arn:aws:s3:::mzla-eks-tb-dev-stalwart-blob-storage"
            - "arn:aws:s3:::mzla-eks-tb-dev-stalwart-blob-storage/*"
        - effect: Deny
          actions:
            - s3:DeleteBucket
            - s3:DeleteBucketPolicy
            - s3:PutBucketAcl
            - s3:PutBucketOwnershipControls
            - s3:PutBucketPolicy
            - s3:PutBucketPublicAccessBlock
            - s3:PutBucketVersioning
            - s3:PutBucketWebsite
            - s3:PutEncryptionConfiguration
            - s3:PutLifecycleConfiguration
          resources:
            - "*"
```

Create a PR with this change, get it approved and merged, and automation will build the role. You'll need this role's ARN later on.


## Create AWS Secrets Manager secrets

A pattern used here (and elsewhere) is to begin with handmade secret resources using AWS Secrets Manager, then the External Secrets Operator (ESO) will come along later and create Kubernetes Secret resources out of them. For this repo, you need two of these.


### Stalwart's recovery admin password

This credential is used to access the admin panel through our Tailscale mesh network. This can be any password you generate securely following best practices. You will also need to come up with a username. Combine these into basic auth format: `$USERNAME:$PASSWORD` to form the `$SECRET` in the following command:

```bash
aws --profile $PROFILE secretsmanager create-secret \
  --name "mzla/$ENVIRONMENT/$NAMESPACE/stalwart-recovery-admin" \
  --secret-string '{"recovery_admin": "$SECRET}'
```


### Stalwart's database credentials

In the [NeonDB Console](https://console.neon.tech) (or however you prefer), either create a new database or a branch from one you want to point this Stalwart deployment at. Acquire the connection details. You'll need the host and username later, but you need the password now to create this secret:

```bash
aws --profile $PROFILE secretsmanager create-secret \
  --name "mzla/$ENVIRONMENT/$NAMESPACE/stalwart-postgresql-admin-credential" \
  --secret-string '{"password": "$NEONDB_PASSWORD}'
```

If you have created a new database, or if the one you have branched is not configured in this way, be sure to restrict access to the database only from the right set of VPC endpoints. These IDs can be found with this command:

```bash
aws --profile $PROFILE ec2 describe-vpc-endpoints \
    --filter 'Name=tag:Name,Values=*-neondb-privatelink-*' \
    --query 'VpcEndpoints[*].VpcEndpointId' \
    --output text
```

If you pull your new Neon project's ID from its settings (this will something like `adjective-verb-abc123`), you can do the restriction by chaining that into a `neon` command:

```bash
for vpce_id in $(
    aws --profile mzla-tb-dev ec2 describe-vpc-endpoints \
        --filter 'Name=tag:Name,Values=*-neondb-privatelink-*' \
        --query 'VpcEndpoints[*].VpcEndpointId' \
        --output text
); do
    neon vpc project restrict $vpce_id \
        --project-id quiet-forest-23207157
done
```


## Copy the Kustomize template

There's a Kustomize template ready for you to clone and fill out, so decide on a descriptive but terse name for your deployment and...

```bash
cp -r overlays/{_template,$DEPLOYMENT_NAME}
```


## Create an ArgoCD application

Pull the latest commits in the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/) and branch it. Find an existing Argo Application manifest for this repo (such as the one at `argocd/tb-dev/apps/thundermail.yaml`) and copy it into the appropriate cluster application directory for your deployment.

- Make any adjustments to the relevant Project manifest to make sure your app is deployable to your chosen cluster and namespace.
- Update this Application manifest:
    - Make sure it has a unique app name.
    - Ensure the `project` reference is correct.
    - Point the Kustomize overlay `path` to the overlay you just created.
    - Ensure the destination server is accurate.
    - Determine a unique namespace to deploy these resources into and update the destination `namespace` field.
    - If you like, you can set the source's `targetRevision` to a working branch while you do the initial buildout.

Create a PR with these changes. Get it reviewed and merged. You can either wait out the auto-sync period (<1 hour), or you can do the relevant refreshes (`argocd-projects` if you made changes to the project file, and the app-of-apps project for your deployment target). Since your overlay hasn't been updated and pushed, this should create your application, but that application should have deployment errors. We expect that right now.


## Update the Kustomize template

The template has many fields you will have to fill out and decisions you'll have to make regarding your deployment (multiple configurations are supported with the right inputs). All of these decisions are clearly documented with the term `SETUP:` and instructions on how to adapt the overlay to your needs.

Run through all of these, setting the right values where necessary and deleting the setup instructions as you go. You will not be able to set everything right away, since many of these fields depend on AWS resources that don't exist yet. Fill out everything you can on a first pass.

Double-check that you have produced valid manifests by building your project:

```bash
kustomize build overlays/$YOUR_OVERLAY
```

Push your changes up. If you are not on a working branch, you'll need to go through with a merge to `main`. Allow ArgoCD and the ACK controllers to build the resources you've requested. Eventually, the stack will error out and be unable to proceed because of missing information.

At this point, do another pass to fill in the missing security group information.
