# Development and Debugging

Here are some common problems that may appear when working through an installation.

- [Kustomize](#kustomize)
- [ACK and AWS Load Balancer Resources](#ack-and-aws-load-balancer-resources)
- [Service Linked Roles for AWS Controllers](#service-linked-roles-for-aws-controllers)
- [Debugging Per-Pod Security Group Issues](#debugging-per-pod-security-group-issues)


## Kustomize

You can build these templates locally by [installing `kustomize`](https://kubectl.docs.kubernetes.io/installation/kustomize/) and running a build command. For example:

    kustomize build overlays/tb-dev

If successful, you should get a series of YAML manifests in the output. If not successful, you will receive a specific error message. These are the same error messages that would surface through ArgoCD if you were to merge and deploy the code, so this is really a requisite development step.

If you want to test builds for all overlays, from the root of this repo, run:

    $ ./util/kustomize-build-all.sh 
    ***** KUSTOMIZE BUILD REPORT *****
    
    -- tb-dev:
    Build status: ✅
    
    -- tb-prod:
    Build status: ✅
    
    Total build failures: 0

This script will run builds for any overlays it finds and alert you if any produce errors. It will output those errors if they occur. However, for successful builds, the script disposes of the actual output. To preserve that, add a directory to the command where the output can be saved.

```bash
mkdir kustomize-builds/
./util/kustomize-build-all.sh kustomize-builds/
```

Remember that a successful build does not necessarily mean you have affected your desired change. Review the manifests before deploying them. You can check what's going to change by saving the output from the above command and running something like the following:

```bash
kubectl diff -f kustomize-builds/tb-dev.yaml
```

The output will show you the diff between the live manifest and the manifest on disk.

This script runs automatically when a PR is opened against this repo, and it must run successfully for the PR to be merged.


## AWS Load Balancer Resources

AWS load balancer resources depend on the normal execution of Kubernetes operators which can reconcile the differences between the declared resources' state in these manifests and their real state in the cloud. This extra component means there are two places you may have to check for debugging information when something goes wrong.

**First,** check the controller logs. You can do this with the ArgoCD web console (locate the "app-of-apps" for your cluster and you'll find the controller pods there). Alternately, with kubectl, first get the pod's full name:

    kubectl -n ack-system get pod

Then pull the logs for review:

    kubectl -n ack-system logs $POD_NAME

These logs reveal problems related to the controller's ability to work within AWS, such as authentication issues.

**Second,** you can look at events on the custom resources themselves, which reveal things like bad configurations and the results of `400 Bad Request` responses from the AWS API. For example, to investigate a security group, you might run:

    kubectl -n thundermail describe service stalwart-admin


## Debugging Per-Pod Security Group Issues

In this project, pods are assigned security groups through `SecurityGroupPolicy` resources. These match pods with certain labels up with security groups you want to attach to them. To find out if something related to a security group is causing a problem, you can launch a debugging container with that label:

```bash
    kubectl -n thundermail run \
        -i --tty --rm debug \
        --image=alpine:latest \
        --restart=Never \
        --labels 'app=stalwart' \
        -- sh
```

You can also use `--labels 'app=stalwart-nginx'` to impersonate an nginx proxy container.

The VPC CNI will assign the appropriate security groups. You can confirm this by looking at the events for your debug pod:

    > kubectl -n thundermail describe pod debug
    
      ...

      Type     Reason                  Age    From                     Message
      ----     ------                  ----   ----                     -------
      Normal   SecurityGroupRequested  6m53s  vpc-resource-controller  Pod will get the following Security Groups [sg-01502e50caccc6795 sg-07f59e7e9ea8b2f0f]



