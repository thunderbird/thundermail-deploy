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

This script will run builds for any overlays it finds and alert you if any produce errors. It will output those errors if they occur. However, for successful builds, the script disposes of the actual output. Remember that a successful build does not necessarily mean you have affected the desired change. Review the manifests before deploying them.

This script runs automatically when a PR is opened against this repo, and it must run successfully for the PR to be merged.


## ACK and AWS Load Balancer Resources

ACK and AWS load balancer resources both depend on the normal execution of Kubernetes operators which can reconcile the differences between the declared resources' state in these manifests and their real state in the cloud. This extra component means there are two places you may have to check for debugging information when something goes wrong.

**First,** check the controller logs. You can do this with the ArgoCD web console (locate the "app-of-apps" for your cluster and you'll find the controller pods there). Alternately, with kubectl, first get the pod's full name:

    kubectl -n ack-system get pod

Then pull the logs for review:

    kubectl -n ack-system logs $POD_NAME

These logs reveal problems related to the controller's ability to work within AWS, such as authentication issues.

**Second,** you can look at events on the custom resources themselves, which reveal things like bad configurations and the results of `400 Bad Request` responses from the AWS API. For example, to investigate a security group, you might run:

    kubectl -n thundermail describe securitygroup stalwart-elasticache-redis


## Service Linked Roles for AWS Controllers

[Service Linked Roles for ACK documentation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.IAM.ServiceLinkedRoles.html) says this about the RDS controller's dependency upon its service linked role:

> You don't need to manually create a service-linked role. When you create a DB instance, Amazon RDS creates the service-linked role for you.
>
> ...
>
> If you delete this service-linked role, and then need to create it again, you can use the same process to recreate the role in your account. When you create a DB instance, Amazon RDS creates the service-linked role for you again.

Though one might expect this role to pop into existence when ACK tries to create a database, it does not.

This project does not rely on RDS, but importantly, it can also happen with Elasticache, which we do rely upon.

There are three sure signs of this problem, demonstrated below.

**The IAM role for your service does not exist.**

Run:

    aws iam get-role --role-name AWSServiceRoleForElastiCache

If you get `aws: [ERROR]: An error occurred (NoSuchEntity) when calling the GetRole operation: The role with name AWSServiceRoleForElastiCache cannot be found.`, then this is your problem.

**The ElastiCache ACK Controller logs show `400`s.**

Check the logs for the ACK Controller for the problem service to see if it reports `400` responses from the AWS API, with the `Missing necessary credentials` reason.

**The custom resource status shows permission errors.**

Run:

    kubectl -n thundermail describe ReplicationGroup stalwart-redis

You have this problem if you see the following message in the status:

    ServiceLinkedRoleNotFoundFault: This action cannot be completed due to insufficient permissions.

**Resolution**

To resolve this, you can create any cache instance at all in Elasticache and then delete it. The role should automatically be created when you create the resource.


## Debugging Per-Pod Security Group Issues

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



