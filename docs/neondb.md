# NeonDB

We use a [Neon database](https://neon.com/) behind an [AWS PrivateLink setup](https://docs.aws.amazon.com/vpc/latest/privatelink/what-is-privatelink.html). Setting this up is a multi-step and entirely manual process [described in full here](https://neon.com/docs/guides/neon-private-networking). Very briefly, this amounts to:

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

