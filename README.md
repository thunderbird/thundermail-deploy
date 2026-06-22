# thundermail-deploy

Manifests for deploying Stalwart-as-Thundermail in Kubernetes.


## Components

### Kustomize

This repo is a [Kustomize](https://kustomize.io/) project. We rely upon the integration of Kustomize with both Kubernetes and [ArgoCD](https://argo-cd.readthedocs.io/en/stable/user-guide/kustomize/) to realize fully applicable Kubernetes manifests which build the resources we need for a deployment.


### ACK

This project makes use of [AWS Controllers for Kubernetes (ACK)](https://aws-controllers-k8s.github.io/community/docs/community/overview/). This allows us to define AWS resources that Stalwart depends on (like security groups or an ElastiCache instance) as Kubernetes manifests. Those manifests define custom Kubernetes resources which describe the properties of those AWS resources.

A [controller](https://github.com/aws-controllers-k8s) must be installed for each service category (EC2, RDS, etc.) in order for these manifests to become real resources. We install these via the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/). As a reference example, [here is the S3 controller installation](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/ack-s3-controller.yaml) for the tb-dev cluster.


### AWS Load Balancer Controller

We use this controller to allow load balanced traffic to our Stalwart pods from outside the network. The controller responds to various conditions in an EKS cluster to create AWS load balancers. In particular, we use an annotation on a `Service` that triggers the creation of a load balancer for mail services.

In the [platform-infrastructure repo](https://github.com/thunderbird/platform-infrastructure/blob/main/argocd/tb-dev/apps/aws-lb-controller.yaml), we install this into the relevant repos using the [Helm installation method](https://docs.aws.amazon.com/eks/latest/userguide/lbc-helm.html). We [annotate a service](./bases/stalwart/service.yaml) to build a load balancer (with listeners and target groups) allowing traffic to our mail services. Valid annotations and the rest of the product's features are [documented here](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/service/annotations/).


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


## Configuration

Once you have a working Stalwart installation, you'll need to fix up a few things in its configuration to reach a baseline point of normal operation. We won't cover full domain setup here, just enough to get Stalwart stable.


### Create and Validate an SSL Certificate

Determine your domain. For this example, we'll use `mail.example.com`.

Using AWS Certificate Manager, request a certificate for your domain:

    aws --profile your-profile acm request-certificate \
        --domain-name '*.example.com' \
        --validation-method DNS \
        --options Export=ENABLED \
        --tags Key=project,Value=thundermail Key=environment,Value=your-env \
        --output CertificateArn

That outputs the ARN of the certificate you requested. Now get the information for the DNS record you need to create to validate the domain:

    aws --profile your-profile acm describe-certificate \
        --certificate-arn $CERTIFICATE_ARN \
        --query 'Certificate.DomainValidationOptions[*].ResourceRecord'

In Cloudflare (or wherever this domain's DNS zone is hosted), create a CNAME Record using those values. In short order, the validation should complete. You can grab the validation status like so:

    aws --profile your-profile acm describe-certificate \
        --certificate-arn $CERTIFICATE_ARN \
        --query 'Certificate.DomainValidationOptions[*].ValidationStatus'

When the status is `SUCCESS`, you are ready to proceed.


### Export and Format the SSL Certificate for Stalwart

As long as you used the `Export=ENABLED` option in the previous step, exporting a certificate is easy enough:

    aws --profile your-profile acm export-certificate \
        --certificate-arn $CERTIFICATE_ARN \
        --passphrase $(echo -n 'password' | base64) \
        --output text \
        --no-cli-pager

The above command will produce a lot of output containing multiple certificates (yours and a CA trust chain) and an encrypted private key. In a temporary directory, copy this data into two files: `example.com.crt` (containing all certificates) and `example.com.key` (containing the encrypted key).

There are two problems now. First, Stalwart wants the certificates in PEM format. Second, Stalwart cannot handle passphrase-encrypted private keys. We must fix both things.

First, strip the passphrase off the RSA key with `openssl`:

    openssl rsa -in example.com.key -out example.com.key

You'll be prompted for the passphrase, which is `password` (or whatever else you may have used in the previous command), and the key file will be replaced by one without a passphrase.

Now reformat the certificate:

    openssl x509 -in example.com.crt -outform PEM -out example.com.crt

You can now use these files in Stalwart.


### Basic Stalwart Network Setup

Log into your Stalwart's admin web console. Here are the known instances:

- [tb-dev](https://mail.dev-thundermail.com/admin/login)
- [tb-prod](https://mail.glamroc.com/admin/login)

You can get the login credentials from the relevant `stalwart-recovery-admin` secret in AWS Secrets Manager.

- Go to Settings ⇢ TLS ⇢ Certificates.
- Click `+ Create certificate`
- Paste the contents of the `.crt` file into the Certificate/Value text box.
- Paste the contents of the `.key` file into the Private Key/Secret text box.
- Click `🖫 Create`.

You now have a TLS configuration you can use elsewhere in the config.

- Go to Settings ⇢ Network ⇢ General.
- Set the default hostname to your domain, the domain covered by the certificate, by clicking the `🔍` icon and selecting the domain from the drop-down.
- Set the default certificate by clicking the `🔍` icon and selecting the certificate you just created.
- Click `🖫 Save`.

Now the configuration is set for Stalwart to present your SSL cert over your domain name, but that configuration is not yet active. To activate it:

- Go to Management ⇢ Actions.
- Click `TLS Certificates`, which will cause all Stalwart services to reload the certificate settings.
- Click `🗲 Execute another action`.
- Click `Server Settings`, which will cause the server settings to reload, referencing the new certificate settings.

Now your change should be live. You can verify in your browser of choice, or by running an `openssl` command to retrieve the cert:

    echo | openssl s_client -connect mail.example.com:443


### Preventing IP Address Blocks

Stalwart manages its own lists of IP addresses to allow or block based on its own criteria. One of those criteria is excessive port scanning. The load balancer does very basic checks of TCP ports to determine the health of the application. To Stalwart, this looks like excessive port scanning, which leads to the internal IPs of the load balancer being blocked by Stalwart. This means no traffic can get through from the load balancer and you have a full service outage on your hands.

To prevent this, create an allowance for your VPC's internal IP range.

In the web console:

- Go to Settings ⇢ Security ⇢ Allowed IPs.
- Click `+ Create address`.
- Enter your VPC's CIDR into the "IP Address(es)" field and type in a helpful description (such as "Allow all internal traffic from the VPC").
- Leave the other fields blank.
- Click `🖫 Create`.

Now traffic from internal systems will always be allowed.


### Resolving IP Address Blocks

As mentioned above, if the load balancer IPs get blocked, you will have all kinds of problems accessing the web console to unblock your load balancer. You will need to create a backdoor to the Stalwart management port and use `stalwart-cli` to create an IP allowance and unblock the blocked IPs.

First, get the IP address of a Stalwart container. It doesn't matter which one, so long as port 8080 is open and connectable.

    # kubectl -n thundermail get pods | grep stalwart
    stalwart-54bf5f6bc6-rhsmv   1/1     Running   0          141m
    
    # kubectl -n thundermail describe pod stalwart-54bf5f6bc6-rhsmv | grep IP
    IP:               10.120.73.236

Now create a new security group rule on your environment's `stalwart-server` security group allowing ingress to port 8080 from your VPC's CIDR. This will enable you to connect from another container on the cluster. We'll delete it later.

Run a live debug container. See [Debugging Per-Pod Security Group Issues](#debugging-per-pod-security-group-issues) for details on the following command, which will connect you to an interactive Alpine Linux container using the `stalwart-server` security group.

    kubectl -n thundermail run \
        -i \
        --tty \
        --rm debug \
        --image=alpine:latest \
        --restart=Never \
        --labels 'app=stalwart' \
        -- sh

In the Alpine container, install `curl`:

    apk add curl

Then install `stalwart-cli` using the [shell installer instructions](https://stalw.art/docs/management/cli/#macos--linux-shell-installer).

    curl --proto '=https' --tlsv1.2 -LsSf \
        https://github.com/stalwartlabs/cli/releases/latest/download/stalwart-cli-installer.sh | sh

Update your `$PATH` to include the installed binary:

    source $HOME/.cargo/env

You can now run `stalwart-cli` commands:

    stalwart-cli --version

Create the allowance first:

    stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        create AllowedIp \
        --field address=$VPC_CIDR' \
        --reason='Allow all internal traffic from the VPC'

Then remove the blocks. To figure out what blocks exist, `query` for `BlockedIp`:

    # stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        query BlockedIp
    
    Id            IP Address(es)  Reason                            Expires At  Created At          
    iwijj9veabqd  10.120.10.169   Excessive port scanning attempts  <none>      2026-06-17T04:06:51Z
    iwg2zu31abad  10.120.29.151   Excessive port scanning attempts  <none>      2026-06-16T21:37:57Z
    iwghlc9kaaqa  10.120.30.223   Excessive port scanning attempts  <none>      2026-06-16T18:30:28Z

You can unblock these by ID, using a comma-separated value:

    # stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        delete BlockedIp \
        --ids iwijj9veabqd,iwg2zu31abad,iwghlc9kaaqa
    
    iwijj9veabqd deleted
    iwg2zu31abad deleted
    iwghlc9kaaqa deleted

Now reload the blocked IPs list:

    # stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        create action/ReloadBlockedIps

    Created Action bvdtkxh

And you should be back up very soon.

Don't forget to delete the temporary rule allowing full VPC CIDR access to `stalwart-server` before you wrap up.


# NeonDB Setup

We use a Neon database behind an AWS PrivateLink setup. Setting this up is a multi-step and entirely manual process [described in full here](https://neon.com/docs/guides/neon-private-networking). Very briefly, this amounts to:

- Create a NeonDB Project for your installation or create a branch from an existing project. If the project is new, create a new database in it called `stalwart`.
- Create a new security group to define network access through the private link you're about to create. You will eventually need to add rules to this that restrict access only from the correct application. Those groups may not exist until you have deployed the application (see [Installation](#installation)), so you may have to come back later and add them.
- Create a VPC endpoint for each one of Neon's service endpoints in your region (see step 4 under ["Create an AWS VPC endpoint"](https://neon.com/docs/guides/neon-private-networking#create-an-aws-vpc-endpoint)), bound to the EKS VPC and the private subnets in it. Specify the security group created in the previous step. **DO NOT enable private DNS at this time.**
- Associate these VPC Endpoint IDs with your Neon organization using the neon CLI tool.
    - [Install neonctl.](https://neon.com/docs/reference/neon-cli#install)
    - Get a list of the IDS of the VPC Endpoints you just created.
    - Grab your organization ID [from here](https://console.neon.tech/app/org-summer-glitter-46282554/settings).
    - Determine the [name of the NeonDB region](https://neon.com/docs/introduction/regions) your project lives in, which will be the AWS region name prefixed with `aws-` (like `aws-eu-central-1`).
    - For each endpoint, run this command (you may be asked to complete an auth flow):
        - `neonctl vpc endpoint assign $your_vpce_id --org-id $neon_org_id --region-id $neon_region_name
- Enable Private DNS on each endpoint.
- Restrict public internet access to the database. Get the NeonDB project ID from the project's settings page (`neonctl projects list` is supposed to give you this, but it appears to not work at time of this writing).
    - `neonctl projects update $NEON_PROJECT_ID --block-public-connections true`
- Restrict which VPC endpoints can access the project. For each VPC endpoint, run a `restrict` command like so:
    - `neonctl vpc project restrict $VPC_ENDPOINT_ID --project-id $NEON_PROJECT_ID`


## Development and Debugging

### Kustomize

You can build these templates locally by [installing `kustomize`](https://kubectl.docs.kubernetes.io/installation/kustomize/) and running a build command. For example:

    kustomize build overlays/tb-dev

If successful, you should get a series of YAML manifests in the output. If not successful, you will receive a specific error message. These are the same error messages that would surface through ArgoCD if you were to merge and deploy the code, so this is really a requisite development step.

If you want to test builds for all overlays, from the root of this repo, run:

```
$ ./util/kustomize-build-all.sh 
***** KUSTOMIZE BUILD REPORT *****

-- tb-dev:
Build status: ✅

-- tb-prod:
Build status: ✅

Total build failures: 0
```

This script will run builds for any overlays it finds and alert you if any produce errors. It will output those errors if they occur. However, for successful builds, the script disposes of the actual output. Remember that a successful build does not necessarily mean you have affected the desired change. Review the manifests before deploying them.


### ACK and Cloudflare Resources

ACK and Cloudflare resources both depend on the normal execution of Kubernetes operators which can reconcile the differences between the declared resources state in these manifests and the real state in the cloud. This extra component means there are two places you may have to check for debugging information when something goes wrong.

**First,** check the controller logs. You can do this with the ArgoCD web console (locate the "app-of-apps" for your cluster and you'll find the controller pods there). Alternately, with kubectl, first get the pod's full name:

    kubectl -n ack-system get pod

Then pull the logs for review:

    kubectl -n ack-system logs $POD_NAME

These logs reveal problems related to the controller's ability to work within AWS, such as authentication issues.

**Second,** you can look at events on the custom resources themselves, which reveal things like bad configurations and the results of `400 Bad Request` responses from the AWS API. For example, to investigate a security group, you might run:

    kubectl -n thundermail describe securitygroup stalwart-elasticache-redis


#### Service Linked Roles for the Elasticache and RDS ACK Controllers

[Service Linked Roles for ACK documentation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAM.ServiceLinkedRoles.html) says this about the RDS controller's dependency upon its service linked role:

> You don't need to manually create a service-linked role. When you create a DB instance, Amazon RDS creates the service-linked role for you.
>
> ...
>
> If you delete this service-linked role, and then need to create it again, you can use the same process to recreate the role in your account. When you create a DB instance, Amazon RDS creates the service-linked role for you again.

Though one might expect this role to pop into existence when ACK tries to create a database, it does not. Related to this project, this problem can also happen with Elasticache.

There are three sure signs of this problem, demonstrated below. The examples given here are for RDS, but you can substitute Elasticache in if that's where your issue is.

**The IAM role for your service does not exist.**

Run:

    aws iam get-role --role-name AWSServiceRoleForRDS

If you get `aws: [ERROR]: An error occurred (NoSuchEntity) when calling the GetRole operation: The role with name AWSServiceRoleForRDS cannot be found.`, then this is your problem.

**The RDS ACK Controller logs show `400`s.**

Check the logs for the ACK Controller for the problem service to see if it reports `400` responses from the AWS API, with the `Missing necessary credentials` reason.

**The custom resource status shows permission errors.**

Run:

    kubectl -n thundermail describe dbinstance stalwart-postgresql

You have this problem if you see the following message in the status:

    ServiceLinkedRoleNotFoundFault: This action cannot be completed due to insufficient permissions.

**Resolution**

To resolve this, you can create any database at all in RDS or cache in Elasticache and then delete it. The role should automatically be created when you create the resource.


### Debugging Per-Pod Security Group Issues

In this project, pods are assigned security groups through `SecurityGroupPolicy` resources which match pods with the `app=stalwart` label to the ID for the `stalwart-server` security group. To find out if something related to a security group is causing a problem, you can launch a debugging container with that label:

    kubectl -n thundermail run \
        -i \
        --tty \
        --rm debug \
        --image=alpine:latest \
        --restart=Never \
        --labels 'app=stalwart' \
        -- sh

The VPC CNI will assign the appropriate security groups. You can confirm this by looking at the events for your debug pod:

    > kubectl -n thundermail describe pod debug
    
      ...

      Type     Reason                  Age    From                     Message
      ----     ------                  ----   ----                     -------
      Normal   SecurityGroupRequested  6m53s  vpc-resource-controller  Pod will get the following Security Groups [sg-01502e50caccc6795 sg-07f59e7e9ea8b2f0f]



