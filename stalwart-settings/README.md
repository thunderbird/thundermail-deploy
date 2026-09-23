# Stalwart Settings

Stalwart does not have a Terraform/Pulumi module (yet?), and (beginning with v0.16) the config file cannot contain anything beyond a database configuration. Beyond that, all settings are stored in the database and manipulated using the [stalwart-cli tool](https://stalw.art/docs/management/cli/). This is used to browse and manipulate the [object schema](https://stalw.art/docs/ref/) without having to understand the internal database schema or the specifics of how to communicate with the Stalwart management API.

In order to store server configurations into files in a git repo, you could write a script with a series of `stalwart-cli` calls in it, but the *prescribed* way is to form an NDJSON file describing all of the operations to perform against a server to bring it into configuration, and applying that with a `stalwart-cli apply` command as described in the [Declarative Bulk Operations docs](https://stalw.art/docs/management/cli/apply/).

However, the NDJSON files must contain exactly one JSON object per line, which means you can't use well formatted JSON in them. This makes the files hard to read and write. JSON files also do not support comments (in the programming sense). For this reason, we have a scheme here where we instead write normal JSON files with each of the `stalwart-cli` instructions being an entry in an array. This later gets interpreted by a script (`util/convert-stalwart-settings-json.py`) which removes `__comment` fields and rewrites the content as minified NDJSON.

This directory contains `.json` files for each of our Stalwart deployments with codified configurations. Match the filename here to the name of the Kustomize overlay for the deployment. To apply a configuration, run the conversion script to generate the `.ndjson` file, then [run a debug container](https://github.com/thunderbird/thundermail-deploy/blob/main/docs/migration-tools.md#setting-up-a-working-container) in a location with access to your deployment's management API. Export the connection settings (the environment variables `STALWART_URL`, `STALWART_USER`, and `STALWART_PASSWORD`) and use `kubectl cp` to copy the right `.ndjson` file to the container. Then apply the settings with:

```bash
stalwart-cli apply --file your-environment.ndjson
```

Your config may additionally require that you supply secrets in the form of environment variables. If that's the case, be sure to export those before running `stalwart-cli`.
