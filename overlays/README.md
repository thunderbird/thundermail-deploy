# Overlay-specific Stalwart configuration

Create a dedicated Stalwart configuration overlay beneath the deployment that
owns it. Keep this overlay separate from the main mail deployment so only the
manual Kargo promotion path can deploy the apply Job.

## Add Stalwart configs to an overlay

In `overlays/<deployment>/stalwart-configs/kustomization.yaml`, add the Stalwart
base as a resource and generate the target values ConfigMap:

```yaml
resources:
  - ../../../bases/stalwart-configs

configMapGenerator:
  - name: stalwart-config-values
    files:
      - values.yaml=config/values.yaml
```

Set the namespace explicitly. The Stalwart base intentionally uses
`namespace: PLACEHOLDER`, so building the base by itself is not deployable:

```yaml
namespace: thundermail-authdev
```

Edit `config/values.yaml` for the deployment's non-secret Stalwart settings:

```yaml
configMapGenerator:
  - name: stalwart-config-values
    files:
      - values.yaml=config/values.yaml
```

The complete setup is in
`overlays/tb-dev-thundermail-authdev/stalwart-configs/kustomization.yaml`.

## What the values control

The values file is packaged into `stalwart-config-values-*`; Kustomize updates
the Job's hashed ConfigMap reference when it changes.

## ConfigMap behavior

The base and overlay generate immutable ConfigMaps:

- `stalwart-config-base-tpl-*`
- `stalwart-config-values-*`

Their names include a deterministic content hash. When their contents change,
Kustomize generates new names and updates the Job volume references.

## Apply-time secret rendering

The Job renders the values ConfigMap and injects private runtime material:

- `stalwart-recovery-admin` supplies CLI username and password.
- `stalwart-cloudflare-api-token` supplies the Cloudflare API token.
- `ssl-certificate` supplies `tls.crt` and `tls.key`.

The resulting NDJSON plan exists only in the Job's `emptyDir` volume.

The CLI currently connects directly to the `stalwart-admin` Kubernetes Service
with `--insecure`. TLS still encrypts the connection, but the CLI does not
verify the server identity because the public certificate does not match the
Service DNS name. Before removing `--insecure`, provision a trusted internal
hostname covered by the certificate (or an internal CA path supported by the
CLI) and update `STALWART_URL` to that verified endpoint.

Do not add `bases/stalwart-configs` to the shared `bases/kustomization.yaml` or
the parent deployment overlay. Either change would bypass the manual Kargo
gate by putting the apply Job in the main mail Application.

## Validate an overlay

From the repository root:

```bash
kustomize build overlays/tb-dev-thundermail-authdev/stalwart-configs
```

To inspect only the Stalwart resources:

```bash
kustomize build overlays/tb-dev-thundermail-authdev/stalwart-configs |
  yq eval '
    select(
      (.kind == "Job" and .metadata.name == "stalwart-config-apply") or
      (
        .kind == "ConfigMap" and
        (
          (.metadata.name | test("^stalwart-config-base-tpl-")) or
          (.metadata.name | test("^stalwart-config-values-"))
        )
      )
    )
  ' -
```

## Rendered manifest branch

The dedicated Stalwart config overlay used by the Kargo promotion pipeline is:

```text
overlays/tb-dev-thundermail-authdev/stalwart-configs
```

After manual promotion, Kargo renders this overlay with `kustomize-build` and
replaces the contents of:

```text
env/tb-dev-stalwart-configs
```

The Argo CD Application tracks that stable branch at path `.`. The branch
contains Kubernetes manifests, not Kustomize source, and advances only when a
reviewer manually promotes selected Git Freight.

No secret values are written to Git. The rendered manifests contain the
template and values ConfigMaps, apply Job, and the SecurityGroupPolicy that
gives the Job access to Stalwart's HTTPS API. The existing
`stalwart-recovery-admin` Secret exposes the composite credential plus separate
username and password keys for the shellless Stalwart CLI image. The apply Job
also uses the existing `stalwart-cloudflare-api-token` Secret and mounts the
existing `ssl-certificate` Kubernetes TLS Secret. The render init container
injects the Cloudflare token and the TLS `tls.crt`/`tls.key` values into the
plan at render time; these private values are not stored in the generated
config Secret or Git.
