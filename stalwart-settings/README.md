# Stalwart Settings

Stalwart does not have a Terraform/Pulumi module (yet?), and (beginning with v0.16) the config file cannot contain anything beyond a database configuration. Beyond that, all settings are stored in the database and manipulated using the [stalwart-cli tool](https://stalw.art/docs/management/cli/). This is used to browse and manipulate the [object schema](https://stalw.art/docs/ref/) without having to understand the internal database schema or the specifics of how to communicate with the Stalwart management API.

In order to store server configurations into files in a git repo, you could write a script with a series of `stalwart-cli` calls in it, but the *prescribed* way is to form an NDJSON file describing all of the operations to perform against a server to bring it into configuration, and applying that with a `stalwart-cli apply` command as described in the [Declarative Bulk Operations docs](https://stalw.art/docs/management/cli/apply/).

However, the NDJSON files must contain exactly one JSON object per line, which means you can't use well formatted JSON in them. This makes the files hard to read and write. JSON files also do not support comments (in the programming sense). For this reason, we have a scheme here where we instead write normal JSON files with each of the `stalwart-cli` instructions being an entry in an array. This later gets interpreted by a script (`util/convert-stalwart-settings-json.py`) which removes `__comment` fields and rewrites the content as minified NDJSON.

This directory contains `.json` files for each of our Stalwart deployments with codified configurations. Match the filename here to the name of the Kustomize overlay for the deployment. To apply a configuration, run the conversion script to generate the `.ndjson` file, then [run a debug container](https://github.com/thunderbird/thundermail-deploy/blob/main/docs/migration-tools.md#setting-up-a-working-container) in a location with access to your deployment's management API. Probably your command should look something like this:

```bash
kubectl -n your-deployment-namespace \
    run -it \
    --image alpine:latest \
    -l app=stalwart \
    debug -- /bin/sh
```

The docs linked above also describe setting `STALWART_*` environment variables. Verify that this is all working with a simple query:

```bash
stalwart-cli query NetworkListener
```

Copy the `.ndjson` file you need to apply into the working container:

```bash
kubectl -n your-deployment-namespace \
    cp stalwart-settings/your-deployment.ndjson \
    debug:/tmp/your-deployment.ndjson
```

And apply the file:

```bash
stalwart-cli apply --file /tmp/your-deployment.ndjson
```

Your config may additionally require that you supply secrets in the form of environment variables. If that's the case, be sure to export those before running `stalwart-cli`.


## Specific Environments' Requirements

This section is intended to inventory our various environments and how to organize data for a successful application of settings to those envs. Find the section below with your env's .json file as the title and ensure you have all the right files and environment variables in place on your run. If you make changes to these .json files, please keep this document up to date.


### All Environments

At the very least, your `.env` file must have these variables set in order to auth against the correct Stalwart server.

```bash
# CoreDNS will resolve this to the mgmt endpoint in the same
# namespace, so this usually works as a URL:
export STALWART_URL='https://stalwart-mgmt'
export STALWART_USER=''
export STALWART_PASSWORD=''
```

The credentials you need for the user/password variables can be found in AWS Secrets Manager, usually at `mzla/{cluster_name}/{deployment_name}/stalwart-recovery-admin`.

Sections below describe deviations from this pattern and additional configurations for each environment.


### tb-dev-thundermail-tbirdemail.json

#### Mounts

You must mount the cert and key for the `tbird.email` domain to `/tmp/tbird.email.crt` and `/tmp/tbird.email.key`.


#### Sample Utility Command

```bash
./util/deploy-stalwart-settings.py \
    --namespace $DEPLOYMENT_NAMESPACE \
    --json ./stalwart-settings/tb-dev-thundermail-tbirdemail.json \
    --mount $PATH_TO_LOCAL_CERT_FILE:/tmp/tbird.email.crt \
    --mount $PATH_TO_LOCAL_KEY_FILE:/tmp/tbird.email.key \
    --env-file .env
```
