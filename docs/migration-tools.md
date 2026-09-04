# Migration Tools

The [Migration](./migration.md) page documents approaches to migrating Stalwart past v0.15. It references many tools, some of which have been specifically designed to solve for this problem. This page distills some concrete examples of how to work with this system.


## Setting Up a Working Container

You will need to run these commands from a system that has access to both environments. Today, that means running a container with shell access to impersonate a Stalwart Migration Proxy container (which must, by definition, be able to speak with all Stalwart installations, as well as its own Redis instance). This is accomplished by applying the `app=stalwart-migration-proxy` label.

```bash
kubectl -n stalwart-migration-proxy run -it \
    --image alpine:latest \
    -l app=stalwart-migration-proxy \
    debug -- /bin/sh
```

You will want to install some package dependencies:

```bash
apk add curl jq py3-virtualenv redis vim
```

Install Vandelay:

```bash
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/stalwartlabs/vandelay/releases/latest/download/vandelay-installer.sh | sh
```

Install Stalwart CLI:

```bash
curl --proto '=https' --tlsv1.2 -sSfL https://github.com/stalwartlabs/cli/releases/latest/download/stalwart-cli-installer.sh | sh
```

Place `vandelay` and `stalwart-cli` in your `$PATH`:

```bash
source ~/.cargo/env
```

Create a dotenv file, filling out the following template:

```bash
export STALWART_URL=http://stalwart-mgmt.thundermail.svc.cluster.local:8080 # Internal mgmt endpoint
export STALWART_USER="<FUTURE SERVER RECOVERY ADMIN USERNAME>"
export STALWART_PASSWORD="<FUTURE SERVER RECOVERY ADMIN PASSWORD>"
export OLD_STALWART_URL=https://mailstrom-stage-management-i.stage-thundermail.com:8080 #Internal legacy mgmt endpoint
export OLD_STALWART_USER="<LEGACY SERVER ADMIN USERNAME>"
export OLD_STALWART_PASSWORD="<LEGACY SERVER ADMIN PASSWORD>"
export BEARER_TOKEN="<MIGRATION PROXY API BEARER TOKEN>"
```

Load these variables into your working shell:

```bash
source ~/.env
```

## Vandelay

[Full documentation.](https://github.com/stalwartlabs/vandelay/)

We would primarily use this to do JMAP-based imports and exports. An import command looks like this:

```bash
# "Import" from the old server
vandelay import jmap \
    --url $OLD_STALWART_URL \
    --auth-basic $OLD_STALWART_USER \
    --auth-password $OLD_STALWART_PASSWORD \
    --account-name 'someuser@thundermail.com' # User whose data is being migrated
    username.sqlite # File on disk storing the data
```

And the other side of the operation looks very similar:

```bash
# "Export" to the new server
vandelay export \
    --url $STALWART_URL \
    --auth-basic $STALWART_USER \
    --auth-password $STALWART_PASSWORD \
    --account-name 'someuser@thundermail.com' # User whose data is being migrated
    username.sqlite # File on disk storing the data
```


## v0.15 API Calls

`stalwart-cli` doesn't have support for the v0.15 API, which has a completely different system of resource organization. If you need to call the old API, you can do so with `curl` using basic auth. As an example, the following call retrieves a list of domains on the legacy system:

```bash
curl -u "$OLD_STALWART_USER:$OLD_STALWART_PASSWORD" \
    "$OLD_STALWART_URL/api/principal?types=domain"
```

[Full documentation on the API is here.](https://stalw.art/docs/0.15/api/management/endpoints/)


## Stalwart Migration Proxy (SMP)


### SMP API

[Full documentation.](https://stalw.art/docs/migration/proxy/management/api/)

SMP exposes a small API which we can use to instruct it which destination should handle traffic for an account. The URLs in the following examples refer to `stalwart-migration-proxy-api`, which is a ClusterIP in the same namespace as SMP; this will be valid as-is for any Stalwart installations made by this deploy repo. Because it's internal, we use the `-k` option to accept unsecure TLS connections and avoid errors.

Get the status of an account mapping:

```bash
curl -k \
  -H 'Authorization: Bearer $BEARER_TOKEN' \
  'https://stalwart-migration-proxy-api:8080/mappings?identifier=account@domain.tld' \
  2> /dev/null | jq .
```

Force an account onto a specific backend:

```bash
curl -k -X PUT \
  -H 'Authorization: Bearer $BEARER_TOKEN' \
  'https://stalwart-migration-proxy-api:8080/mappings?identifier=account@domain.tld'
```

Delete an account mapping, forcing it onto the default backend:

```bash
curl -k -X DELETE \
  -H 'Authorization: Bearer $BEARER_TOKEN' \
  'https://stalwart-migration-proxy-api:8080/mappings?identifier=account@domain.tld'
```

### SMP Redis

SMP keeps two copies of its state. One exists in a local cache on each individual container running SMP. When you make a call like the ones above, SMP checks this local cache first and responds based on what it finds there. Only on a cache miss does it check its Redis cache.

On the one hand, this supports the use of stateless clustering such as what's defined in this repo. On the other hand, we have seen this cause some problems. Specifically, after running the `PUT` call to migrate a test account over, if you make the `DELETE` call thinking that will force it back to the default, you may be disappointed. It seems SMP only deletes its own internal cache entry for that mapping, leaving the Redis cache entry intact. If a future `GET` call comes in to a different host, say one that has no local cache entry, it will check Redis and determine that the account should still be routed to the non-default backend. This might be a bug. The only way (that I know of) to clear the internal cache is to bounce the container.

Because problems like this exist, you may want to check in on the Redis state yourself. In our deployment, there's an ExternalName service for convenience called `stalwart-migration-proxy-redis`, which we use in the following examples.

Connect to SMP's Redis host:

```bash
redis-cli -h stalwart-migration-proxy-redis
```

If you were to run a key scan (which you should be cautious to do in a production system with many entries, since this can create performance issues in Redis), you would see that the key names are extremely predictable. They come in the form of `route:$account_name` where `$account_name` is typically an email address.

So to get the contents of an account mapping, just run the Redis query:

```
stalwart-migration-proxy-redis:6379> GET route:user@domain.tld
1) "tb-dev-thundermail"
```

That value should match the ID of a destination in the SMP config, indicating which backend is to serve the request.

You can run a Redis query and then disconnect without invoking the redis-cli subshell:

```
/ # echo 'GET route:user@domain.tld' | redis-cli -h stalwart-migration-proxy-redis
tb-dev-thundermail
```
