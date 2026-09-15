# thundermail-deploy Repo Structure

## Directory Outline

- `kustomization.yaml`: Root configuration for the Kustomize configuration. Seeds the inventory for
    the base manifests.
- `bases`: Contains the base manifests to Kustomize. Key environment-specific information is omitted in these files.
    - `nginx`: Contains the base manifests describing the nginx proxy for Stalwart
    - `stalwart-mail`: Contains the base manifests describing the Stalwart installation
- `docs`: This project's documentation.
- `overlays`: Contains Kustomize patches for different installations. See ["Overlay Structure"](#overlay-structure) below.
- `util`: Contains offhand scripts useful to this repo


## Overlay Structure

In the `overlays` directory, you'll find more directories named after installations of this project. Each of these directories should contain YAML files with patches that environmentalize the install. These files follow the same organization as the `bases` directory, though this is a convention used to improve readability of the code and not a hard requirement. The `kustomization.yaml` file should list each of these files as patches.

Each overlay file should contain enough information to uniquely identify the resource it is patching. At minimum, you should include both the name of the resource and the namespace it lives in.

New deployments are created in part by copying the contents of `_template` and making adjustments, but you should really step through the [New Deployment docs](./new-deployments.md) if you intend on doing that.
