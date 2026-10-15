# Runbook for Stalwart Migration from v0.15.x to v0.16.x

> [!NOTE]
> Note that many of the tickets linked here refer to "tb-dev" but that we are likely going to change that to a "tb-stage" deployment. The work remains the same, only the names change.


## Preamble

We need to upgrade Stalwart from v0.15.x to v.0.16.x because this is the future of all Stalwart development, and the new version provides us with a lot of new features and benefits. The process is complicated by a variety of things, including changes to the API and schema which make this migration non-automatic. There are two main methods we can use to accomplish this, documented [here](https://github.com/thunderbird/thundermail-deploy/blob/main/docs/migration.md).

Due to the additional development and engineering lift of the "no-downtime" approach and the relative [short execution time](https://github.com/thunderbird/platform-infrastructure/issues/1156#issuecomment-5706616195) of the "downtime-inducing" approach, we have elected to accept the downtime and perform an offline maintenance to upgrade Stalwart.

Changes to the way Stalwart is configured and how significant migrations are to be done in the future, we have new software components (Stalwart Migration Proxy, nginx) in the mix, and we need to confirm the functionality of our mail services against this new infra and on the new Kubernetes deployment platform before we do the migration.


## Pre-Migration Steps


### Pre-Migration Validation of Services and Infrastructure

The purpose of these steps is to make sure that -- regardless of whether we are taking traffic for a development, staging, or production deployment -- the new version of Stalwart works against our new infrastructure with the new software components in place.

- Set up a working Stalwart deployment using a unique domain name. This needs to be [hooked up to an Accounts API/Customer Keycloak system](https://github.com/thunderbird/platform-infrastructure/issues/792) seeded with test data from legacy-stage so that we can test everything.
- [Confirm basic functionality](https://github.com/thunderbird/platform-infrastructure/issues/758) of the mail server through happy path testing just to ensure there are no broad problems that still need to be resolved at the infrastructure or configuration level.
- [Run a full test suite against this Stalwart/Auth/Accounts combo](https://github.com/thunderbird/platform-infrastructure/issues/761). Discover and resolve issues related to this.

At this point, we can be confident in a baseline configuration. We may need to make environment-specific adjustments for the stage and prod migrations, but we know what a working configuration looks like and we know that the proxying through the SMP and nginx are working.


### Pre-Migration Steps for Stage

- [Deploy a thundermail-deploy instance](https://github.com/thunderbird/platform-infrastructure/issues/1170) to the tb-stage cluster under the `thundermail-stage` namespace using the `stage-thundermail.com` domain.
- Bring the tb-stage deployment's blob storage bucket into data parity with legacy-stage's with [cross-account S3 bucket replication](https://github.com/thunderbird/platform-infrastructure/issues/1171) and [a batch replication operation](https://github.com/thunderbird/platform-infrastructure/issues/1172).
- Configure the tb-stage/thundermail deployment to receive a failover from legacy-stage by [nuking the DB](https://github.com/thunderbird/platform-infrastructure/issues/1173) and [applying our prefered settings](https://github.com/thunderbird/platform-infrastructure/issues/1174).
- Establish VPC peering between legacy-stage and tb-stage (**No issue for this yet.**) This can be done entirely with Pulumi. See [these peering requests in tb-dev](https://github.com/thunderbird/platform-infrastructure/blob/main/pulumi/environments/mzla-tb-dev/config.prod.yaml#L36-L64) and [these accepting requests and special routes](https://github.com/thunderbird/mailstrom/blob/main/pulumi/config.stage.yaml#L43-L56) in legacy-stage as working examples.
- Ensure that the Stalwart Migration Proxy pod in tb-stage is able to communicate with both the legacy-stage admin API and the tb-stage deployment's management API. (**No issue for this yet.**) The peering above is one part, but the SMP pod's security group must also be permitted access to the legacy API.


### Pre-Migration Steps for Prod

- Resolve issues related to IPAM. We use IPAM in legacy-prod to ensure we always have a reputable set of IPs that all our emails can come from. These get attached to a NAT Gateway and then all Stalwart egress is passed through them. We've been running on a set of these in legacy-prod that I believe we'd like to bring over to tb-prod. We have a couple options here, and should choose one:
    - [Migrate the old IPAM addresses to the new account, or grant access to them in some way](https://github.com/thunderbird/platform-infrastructure/issues/416)
    - [Build a fresh IPAM pool for tb-prod](https://github.com/thunderbird/platform-infrastructure/issues/1314)
- There are a few data validation errors that come up when we run the migration steps. Those have been [documented here](https://github.com/thunderbird/platform-infrastructure/issues/1165). We should investigate how we get to the point where this invalid data is permitted in our system, determine the correct fix to place in the current v15 system so that the validation errors no longer occur. We should probably also address any bugs in Accounts (or wherever else) that might be resulting in these invalid configurations, thereby preventing any new issues from arising post-migration.
- [Deploy a thundermail-deploy instance](https://github.com/thunderbird/platform-infrastructure/issues/1178) to the tb-prod cluster under the `thundermail-prod` namespace using the `-thundermail.com` domain.
- Bring the tb-prod deployment's blob storage bucket into data parity with legacy-prod's with [cross-account S3 bucket replication](https://github.com/thunderbird/platform-infrastructure/issues/1179) and [a batch replication operation](https://github.com/thunderbird/platform-infrastructure/issues/1180).
- Configure the tb-prod/thundermail deployment to receive a failover from legacy-prod by [nuking the DB](https://github.com/thunderbird/platform-infrastructure/issues/1181) and [applying our prefered settings](https://github.com/thunderbird/platform-infrastructure/issues/1182).
- Ensure that the Stalwart Migration Proxy pod in tb-prod is able to communicate with both the legacy-prod admin API and the tb-prod deployment's management API. (**No issue for this yet.**) The peering above is one part, but the SMP pod's security group must also be permitted access to the legacy API.
- Work with Support to schedule a maintenance period for the migration event and communicate this to our users.


## Migration Steps

These steps can be untertaken when the pre-migration steps above are complete for an environment. Ensure that S3 object replication is up to date before beginning. Some specifics (URLs, bucket names, etc.) may differ between environments, but the steps remain the same.


### Day-of Prep

Launch a working container in a network location that has access to both systems, the source (legacy-stage/legacy-prod) and the target (tb-stage/thundermail, tb-prod/thundermail). In tb-dev, this is achieved by impersonating an SMP pod. The same can be configured in other environments as well through VPC peering and security group allowances. Applying the label shown below ensures the working container gets the same security group as a real SMP pod.

```bash
kubectl -n stalwart-migration-proxy run -it \
    --image alpine:latest \
    -l app=stalwart-migration-proxy \
    debug -- /bin/sh
```

Install some package dependencies for running the migration and troubleshooting, and set up `stalwart-cli`:

```bash
apk add busybox-extras curl jq net-tools postgresql py3-virtualenv redis vim
curl --proto '=https' --tlsv1.2 -sSfL https://github.com/stalwartlabs/cli/releases/latest/download/stalwart-cli-installer.sh | sh
source ~/.cargo/env
```

Create a dotenv file, filling out the following template:

```bash
export STALWART_URL=http://stalwart-mgmt:8080 # Internal mgmt endpoint for target system
export STALWART_USER="<FUTURE SERVER RECOVERY ADMIN USERNAME>"
export STALWART_PASSWORD="<FUTURE SERVER RECOVERY ADMIN PASSWORD>"
export OLD_STALWART_URL=https://mailstrom-stage-management-i.stage-thundermail.com:8080 #Internal legacy mgmt endpoint
export OLD_STALWART_USER="<LEGACY SERVER ADMIN USERNAME>"
export OLD_STALWART_PASSWORD="<LEGACY SERVER ADMIN PASSWORD>"
export BEARER_TOKEN="<MIGRATION PROXY API BEARER TOKEN>"
```

In prod, set `OLD_STALWART_URL="https://mailstrom-prod-management-i.thundermail.com:8080"` instead.

Load these variables into your working shell:

```bash
source ~/.env
```

Download the migration script and prepare a Python virtual environment for it:

```bash
cd
curl -fLO https://raw.githubusercontent.com/stalwartlabs/stalwart/refs/heads/main/resources/scripts/migrate_v016.py
virtualenv venv
source venv/bin/activate
pip install requests urllib3
```

Disconnect public access to the v0.15 system so that users cannot connect and continue to induce changes in the state. (**How? We can route DNS to a dead end or something. Should do a quick spike or chat about this detail.**) Downtime begins now.

Export all content from the source system. Adjust the command below for the appropriate environment:

```bash
python migrate_v016.py dump \
    --url https://mailstrom-stage-management-i.stage-thundermail.com:8080 \
    --username admin \
    --password adminPassword \
    --settings settings.json \
    --principals principals.json
```

This produces the `settings.json` and `principals.json` files, which now need to be converted to a format compatible with v0.16.x:

```bash
python migrate_v016.py convert \
    --settings settings.json \
    --principals principals.json \
    --config config.json \
    --output export.json
```

This produces the `config.json` file, which we will not use. This is because this contains the database connection info *for the legacy system* when we want to use a different database in the new system. That DB connection is already codified and will be applied on a container restart.

It also produces the `export.json` file, which is the NDJSON file containing all the domains, accounts, etc. from the legacy system. You can find a comprehensive list of its contents [here](https://github.com/stalwartlabs/stalwart/blob/main/UPGRADING/v0_16.md#what-the-script-converts--and-what-it-does-not) We now need to import this into the new system:

```bash
stalwart-cli apply -f export.json
```

For good measure, we now re-apply our own prefered settings.

> [!NOTE]
> I have been using a script to deploy settings from NDJSON files manually. There is now a new way to do this where the settings are packaged as YAML and part of the K8s manifests. I am not sure what the process is for manually applying these settings under this model. Should write that out here.

And finally issue a restart of the Stalwart containers to ensure they start up correctly:

```bash
kubectl -n thundermail rollout restart stalwart
```

At this point, the server should come online, and we can do a spot-check of all the things that matter most:

- The servers are pointing at the correct database via the `config.json` file that comes from the pod's ConfigMap.
- The servers are using the correct blob store (the S3 bucket created for this purpose in the target environment).
- The servers are using the correct in-memory store (the Redis instance in the target environment).
- The expected domains and accounts are present.

If access to the servers was cut off before, we can now restore access to the service for further testing. For good measure, we can run E2E tests to confirm things are working.

The migration is now complete.
