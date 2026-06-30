# Configuration

Once you have a working Stalwart installation, you'll need to fix up a few things in its configuration to reach a baseline point of normal operation. We won't cover full domain setup here, just enough to get Stalwart stable.


- [Create and Validate an SSL Certificate](#create-and-validate-an-ssl-certificate)
- [Export and Format the SSL Certificate for Stalwart](#export-and-format-the-ssl-certificate-for-stalwart)
- [Basic Stalwart Network Setup](#basic-stalwart-network-setup)
- [Preventing IP Address Blocks](#preventing-ip-address-blocks)
- [Resolving IP Address Blocks](#resolving-ip-address-blocks)


## Create and Validate an SSL Certificate

Determine your domain. For this example, we'll use `mail.example.com`.

Using AWS Certificate Manager, request a certificate for your domain:

    aws --profile $AWS_PROFILE acm request-certificate \
        --domain-name '*.example.com' \
        --validation-method DNS \
        --options Export=ENABLED \
        --tags Key=project,Value=thundermail Key=environment,Value=your-env \
        --query CertificateArn

That outputs the ARN of the certificate you requested. Now get the information for the DNS record you need to create to validate the domain:

    aws --profile $AWS_PROFILE acm describe-certificate \
        --certificate-arn $CERTIFICATE_ARN \
        --query 'Certificate.DomainValidationOptions[*].ResourceRecord'

In Cloudflare (or wherever this domain's DNS zone is hosted), create a CNAME Record using those values. In short order, the validation should complete. You can grab the validation status like so:

    aws --profile $AWS_PROFILE acm describe-certificate \
        --certificate-arn $CERTIFICATE_ARN \
        --query 'Certificate.DomainValidationOptions[*].ValidationStatus'

When the status is `SUCCESS`, you are ready to proceed.


## Export and Format the SSL Certificate for Stalwart and Nginx

As long as you used the `Export=ENABLED` option in the previous step, exporting a certificate is easy enough:

    aws --profile $AWS_PROFILE acm export-certificate \
        --certificate-arn $CERTIFICATE_ARN \
        --passphrase $(echo -n 'password' | base64) \
        --output text \
        --no-cli-pager

The above command will produce a lot of output containing multiple certificates (yours and a CA trust chain) and an encrypted private key. In a temporary directory, copy the key data into a file, `example.com.key` (containing the encrypted key).

AWS Certificate Manager requires that you encrypt the key with a passphrase for export, but Stalwart cannot handle passphrase-encrypted private keys. Fix this by stripping the passphrase off the RSA key with `openssl`:

    openssl rsa -in example.com.key -out example.com.key

You'll be prompted for the passphrase, which is `password` (or whatever else you may have used in the previous command), and the key file will be replaced by one without a passphrase.


### Using the certificate in Nginx

AWS Secrets Manager doesn't allow for multiline secrets. It converts newline characters into spaces. PEM certificates require newline characters throughout. This means we can't use Secrets Manager to deliver a certificate to Nginx via an ExternalSecret resource like we do for other secret values.

In order to use this cert in the nginx config, we have to manually create a TLS secret called `ssl-certificate` in the `thundermail` namespace. Assuming your cert and key can be found at `/tmp/tls.crt` and `/tmp/tls.key`, you should run this command:

    kubectl -n thundermail create secret tls ssl-certificate \
        --cert /tmp/tls.crt \
        --key /tmp/tls.key

Nginx is configured to use the secret and keys this command generates.


### Using the certificate in Stalwart

To use the certificate:

- In the Stalwart web console, go to Settings ⇢ TLS ⇢ Certificates.
- Click `+ Create certificate`.
- Leave the "Certificate" field set to "Text value".
- In the "Value" text entry, enter the certificate for your domain followed by all chain/intermediate certs.
- Leave the "Private key" field set to "Secret value".
- In the "Secret" text entry, enter the private key with the passphrase removed.
- Click `🖫 Create`.

You can now use this certificate in other Stalwart settings, such as the basic network setup.


## Basic Stalwart Network Setup

Log into your Stalwart's admin web console. Here are the known instances:

- [tb-dev](https://mail.dev-thundermail.com/admin/login)
- [tb-prod](https://mail.tb.email/admin/login)

You can get the login credentials from the relevant `stalwart-recovery-admin` secret in AWS Secrets Manager.

First, configure a domain.

- Go to Management ⇢ Domains ⇢ Domains.
- Click `+ Create domain`.
- Enter your domain name and leave all other options at defaults.
- Click `🖫 Create`.

Next, configure a TLS certificate using the exported cert and key from above.

- Go to Settings ⇢ TLS ⇢ Certificates.
- Click `+ Create certificate`
- Paste the contents of the `.crt` file into the Certificate/Value text box.
- Paste the contents of the `.key` file into the Private Key/Secret text box.
- Click `🖫 Create`.

You now have the domain and TLS configuration you need to set up the general network settings.

- Go to Settings ⇢ Network ⇢ General.
- Set the default hostname to your domain, the domain covered by the certificate, by clicking the `🔍` icon and selecting the domain from the drop-down.
- Set the default certificate by clicking the `🔍` icon and selecting the certificate you just created.
- Click `🖫 Save`.

Now the configuration is set for Stalwart to present your SSL cert over your domain name, but that configuration is not yet active. To activate it:

- Go to Management ⇢ Actions.
- Click `TLS Certificates`, which will cause all Stalwart services to reload the certificate settings.
- Click `🗲 Execute another action`.
- Click `Server Settings`, which will cause the server settings to reload, referencing the new certificate settings.

Now your change should be live. You can verify in your browser of choice, or by running an `openssl` command to retrieve the cert:

    echo | openssl s_client -connect mail.example.com:443


## Preventing IP Address Blocks

Stalwart manages its own lists of IP addresses to allow or block based on its own criteria. One of those criteria is excessive port scanning. The load balancer does very basic checks of TCP ports to determine the health of the application. To Stalwart, this looks like excessive port scanning, which leads to the internal IPs of the load balancer being blocked by Stalwart. This means no traffic can get through from the load balancer and you have a full service outage on your hands.

To prevent this, create an allowance for your VPC's internal IP range.

In the web console:

- Go to Settings ⇢ Security ⇢ Allowed IPs.
- Click `+ Create address`.
- Enter your VPC's CIDR into the "IP Address(es)" field and type in a helpful description (such as "Allow all internal traffic from the VPC").
- Leave the other fields blank.
- Click `🖫 Create`.

Now traffic from internal systems will always be allowed.


## Resolving IP Address Blocks

As mentioned above, if the load balancer IPs get blocked, you will have all kinds of problems accessing the web console to unblock your load balancer. You will need to connect to the Stalwart management port and use `stalwart-cli` to create an IP allowance and unblock the blocked IPs.

First, get the IP address of a Stalwart container. It doesn't matter which one, so long as port 8080 is open and connectable.

    # kubectl -n thundermail get pods | grep stalwart
    stalwart-54bf5f6bc6-rhsmv   1/1     Running   0          141m
    
    # kubectl -n thundermail describe pod stalwart-54bf5f6bc6-rhsmv | grep IP
    IP:               10.120.73.236

The `stalwart-server` security group has a rule allowing connections from itself, so you need to run a live debug container that uses it. See [Debugging Per-Pod Security Group Issues](#debugging-per-pod-security-group-issues) for details on the following command, which will connect you to an interactive Alpine Linux container using the `stalwart-server` security group.

    kubectl -n thundermail run \
        -i \
        --tty \
        --rm debug \
        --image=alpine:latest \
        --restart=Never \
        --labels 'app=stalwart' \
        -- sh

In the Alpine container, install `curl`:

    apk add curl

Then install `stalwart-cli` using the [shell installer instructions](https://stalw.art/docs/management/cli/#macos--linux-shell-installer).

    curl --proto '=https' --tlsv1.2 -LsSf \
        https://github.com/stalwartlabs/cli/releases/latest/download/stalwart-cli-installer.sh | sh

Update your `$PATH` to include the installed binary:

    source $HOME/.cargo/env

You can now run `stalwart-cli` commands:

    stalwart-cli --version

Create the allowance first:

    # stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        create AllowedIp \
        --field address=$VPC_CIDR' \
        --field reason='Allow all internal traffic from the VPC'
    
    Created AllowedIp ixf91jeeauqa

Then remove the blocks. To figure out what blocks exist, `query` for `BlockedIp`:

    # stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        query BlockedIp
    
    Id            IP Address(es)  Reason                            Expires At  Created At          
    iwijj9veabqd  10.120.10.169   Excessive port scanning attempts  <none>      2026-06-17T04:06:51Z
    iwg2zu31abad  10.120.29.151   Excessive port scanning attempts  <none>      2026-06-16T21:37:57Z
    iwghlc9kaaqa  10.120.30.223   Excessive port scanning attempts  <none>      2026-06-16T18:30:28Z

You can unblock these by ID, using a comma-separated value:

    # stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        delete BlockedIp \
        --ids iwijj9veabqd,iwg2zu31abad,iwghlc9kaaqa
    
    iwijj9veabqd deleted
    iwg2zu31abad deleted
    iwghlc9kaaqa deleted

Now reload the blocked IPs list:

    # stalwart-cli --url http://$STALWART_CONTAINER_IP:8080/ \
        --user $RECOVERY_USER \
        --password $RECOVERY_PASSWORD \
        create action/ReloadBlockedIps

    Created Action bvdtkxh

And you should be back up very soon.


