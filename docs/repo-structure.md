# thundermail-deploy Repo Structure

## Directory Outline

- `kustomization.yaml`: Root configuration for the Kustomize configuration. Seeds the inventory for
    the base manifests.
- `bases`: Contains the base manifests to Kustomize. Key environment-specific information is omitted in these files.
    - `aws`: Contains the base manifests describing AWS ACK resources.
        - `elasticache.yaml`: Stalwart's Redis cache
        - `kustomization.yaml`: Kustomize inventory for AWS resources
        - `s3.yaml`: Stalwart's blob storage bucket
        - `security-groups.yaml`: Security groups used by other resources, including per-pod security groups
        - `subnet-groups.yaml`: VPC subnet groups for use with managed data stores
    - `stalwart`: Contains the base manifests describing the Stalwart installation
        - `configmap.yaml`: The minimal config file for Stalwart
        - `deployment.yaml`: The Stalwart deployment
        - `kustomization.yaml`: Kustomize inventory for Stalwart resources
        - `secrets.yaml`: Uses `ExternalSecret` resources to import secret data from AWS Secrets Manager
        - `service.yaml`: Kubernetes services exposing Stalwart features
        - `serviceaccount.yaml`: Binds the Stalwart pod to an IAM role via IRSA
- `docs`: This project's documentation.
- `overlays`: Contains Kustomize patches for different installations. See ["Overlay Structure"](#overlay-structure) below.
- `util`: Contains offhand scripts useful to this repo


## Overlay Structure

In the `overlays` directory, you'll find more directories named after installations of this project. Each of these directories should contain YAML files with patches that environmentalize the install. These files follow the same organization as the `bases` directory, though this is a convention used to improve readability of the code and not a hard requirement. The `kustomization.yaml` file should list each of these files as patches.

Each overlay file should contain enough information to uniquely identify the resource it is patching. At minimum, you should include both the name of the resource and the namespace it lives in.
