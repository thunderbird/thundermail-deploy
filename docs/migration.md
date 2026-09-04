# Stalwart Migrations

> [!TIP]
> This document describes a lot of tools in broad terms. For some specific examples of how to interact with these systems, see [Migration Tools](./migration-tools.md)

Ordinarily, we should be able to update a Stalwart deployment without too many problems. You can update an environment's `kustomize.yaml` file with an `images` entry like this:

```yaml
images:
  - name: stalwartlabs/stalwart
    newTag: v0.16.16
```

When that change is merged into the `main` branch, ArgoCD will sync the new version out. When the first container comes up, it will perform any database migrations that need to happen, then stabilize. The new containers will come alive. The old containers will be replaced, and you will have completed the version upgrade. This is all silent and transparent and works almost all of the time.

However, releases of Stalwart beginning at v0.16.x rely on a dramatically different database schema, and there is no such clean upgrade path between prior versions and v0.16. Any such upgrade must begin with an upgrade of Stalwart to v0.15.x. There are two migration paths from there. This document lays out those two paths and weighs their value. It also outlines the tools involved in these processes (some of which are bespoke to this specific upgrade).


## The Two Paths, Broadly


### (Mostly) Downtime-Free Approach

The Stalwart team has developed two tools to help migrate without downtime.

One tool is called Vandelay. It uses various mail protocols to pull data from a mail/DAV server and store that data in a SQLite database file on the local disk. This is called an "import" (in the sense that we are importing data to the SQLite DB). This data is updated one record at a time, and IDs are logged, so that if the procedure is interrupted, it can be restarted without re-running the entire process; it will pick up where it left off. When you have all of this data localized, you can then run the opposite transaction, an "export". In this step, the data from the SQLite file is loaded back into some other server via some other protocol. Since we have Stalwart on both sides of our migration, we can use JMAP for both the import and export.

The other tool is called the Stalwart Migration Proxy (SMP). This is a networking tool that sits in front of any and all Stalwart installations which you may want to migrate accounts between. It serves as the public receptor for mail traffic for all of these installations, called "destinations". Its configuration specifies one of these as the default destination. Upon receiving a connection, SMP determines which of the destinations is intended to handle the traffic by resolving which account it refers to. It checks an internal cache for destination mapping information regarding that account, and then a Redis cache if that local call misses. If it finds a hit, it will route the traffic to the specified destination. If there's no entry in either cache, it will route the traffic to the default destination. Account routing is controlled via a simple REST API it exposes.

At a glance, this process looks like this:

1. Stand up a new Stalwart v0.16.x installation expecting the PROXY protocol headers to come from the internal network that the SMP will be deployed to.
2. Stand up an SMP installation in front of it running on that network.
3. Add both the old v0.15 installation and the new v0.16 installation as destinations in the SMP config.
4. Adjust the settings on the v0.15 installation to expect the same PROXY protocol headers.
5. Swap DNS for the mail domain over to the SMP.
6. Perform account migrations (more on this later, since it's its own procedure worth spelling out).
7. When all accounts have been migrated, remove the old destination from the SMP config.

This procedure is marked as "mostly" downtime free because of Steps 4 and 5. If mail comes through the SMP, it will have the PROXY protocol headers. If Stalwart is not configured to expect them, it will fail to handle the traffic. Even if you do these steps in very quick succession, you still have to account for a full cluster restart (to deliver the settings change) and the propagation of the DNS record. This is maybe 5 minutes of non-operation. In theory, once you have the SMP as the public point of contact, you never need to take this downtime again for future migrations.


### Downtime-Inducing Approach

With this approach, we prepare a "receiving" installation of Stalwart v0.16 to migrate to. We configure the deployment with any special settings we need post-migration. This could include things like a new SSL certificate, new auth configuration, new network listeners, etc.

Prior to the migration, we have to copy all of the data from the originating system's blob store (an S3 bucket) to a similar store used by the new system. This is done by setting up some special IAM permissions on both ends, adding a replication rule to the live system's bucket that automatically copies all new objects to the post-migration system's bucket, and running a one-time batch replication operation that copies all existing objects. At this point, with the historical data copied and all new data being live-copied, the two buckets will remain in sync until the migration occurs. At that point, all new objects will be written to the new bucket and the old bucket will go out of sync.

Database content does not have to be copied or replicated because the migration operation itself will recreate this data. In fact, it **cannot** be copied in that way because there is no direct schema migration path between v0.15 and v0.16. This approach involves recreating all of this data. Assuming the blob store is prepared as described above, here's how we do this:

1. Launch a working container in a network location that has access to both systems. Set up all the necessary software, including the migration scripts we'll be using and system packages to help us.
2. Take a snapshot of the future (v0.16) system's settings which we want to preserve after the migration (new SSL certs and such).
3. Disconnect public access to the v0.15 system so that users cannot connect and continue to induce changes in the state. Downtime begins now.
4. Run the Stalwart-provided export script that extracts data from our v0.15 system.
5. Run the Stalwart-provided migration script that converts that data into a form the v0.16 system can ingest.
6. Deploy changes to the v0.16 system to scale down to a single node running in recovery mode. This disables all mail ports and reduces the system to only the management API.
7. Run two `stalwart-cli` commands, first to apply the v0.15 data to the v0.16 system, then to re-apply the settings we decided earlier to retain from the v0.16 system.
8. Connect to the admin panel for the v0.16 system and verify all settings. Especially important are the blob and in-memory storage settings, which should have been exported and re-applied. The listener config should also reflect the right network setup. At a bare minimum, port 8080/management must be enabled so that we can fix any other problems that might arise.
9. Deploy changes to the v0.16 system to scale back up and disable recovery mode. Update DNS to point to the Stalwart Migration Proxy (assuming we keep this in the installation, else point to whatever the public interface for mail services is).
10. Export the zone file for our mail domain as a backup in case we need to roll back for some reason. Enable automatic DNS management for our domain. This will require a Cloudflare API token, and it will blow away all of the legacy system's DNS settings, but the result should be that our DNS records are now correctly configured for the new system. Downtime ends now.


## Considerations


### True Complexity of the No-Downtime Approach

The main advantage here is that downtime is minimized to a blip as we cut over to using the migration proxy. However, it complicates things significantly because the tools we are given to work with (SMP and Vandelay) are extremely simple tools that have no orchestrating component. Vandelay has no importable library or API-accessible control point; you must shell out commands to move account data. SMP will not issue Vandelay commands. Vandelay will never notify an SMP installation that an account's data has been moved. All of these operations are things we have to develop and orchestrate within our system.

Moreover, we cannot guarantee that an account will not receive mail or perform some kind of move operation while we are migrating its data. This necessitates some operational complexities. I propose that if we were to take this approach and tackle this problem, we do it like this:

- Write a function that handles the orchestration. `@task` decorate this as part of the `thunderbird-accounts` codebase so that we can emit these as Celery tasks on command.
- Bind the task emission to a set of Django admin panel functions that expose the state of the account according to SMP. Allow admins to manually submit tasks per-account on demand. Possibly develop a bulk operation wrapper around this to submit all the tasks at once, or to process them all with some kind of rate limiting to prevent system overload.
- This script would shell out `vandelay` commands in a loop to ensure changes in the account are picked up. You must run an `import` followed by an `export` followed by another `import`. If that import detects changes, you must loop another `export` and `import` until no changes are detected. At this point, the task reports to SMP that the account has been migrated. SMP now routes this account's traffic to the new system and reports to Accounts Admin this new state when queried.
- Between imports, the SQLite database must be backed up to S3 or some other remote store. When a new task launches, it must check to see if one exists in the remote store in order to recover from any failure that might have occurred. When it completes successfully, it must delete this object from remote storage since we've no need to keep it beyond the migration process (reducing $$$ cost).
- The operating environment must have storage available to support importing our largest accounts, lest some tasks get stuck in perpetual failure of disk space limitations.
- We must have a way of knowing that all accounts have been migrated so that we could know when it's safe to tear down the old infrastructure.

There are less "right" ways of doing this, I'm sure (and maybe a more "right" way as well). We could always hack something together in a shell script, but the above process gives us visibility through the whole migration and as much manual control as we could like.

All of this is added complexity that becomes a specific integration in our system to support a migration that theoretically happens only once, or at least very infrequently. One might argue that using SMP gives us flexibility (ability to arbitrarily move accounts between deployments, which might be useful for testing or canarying), but that does not imply we need to use this approach right now for this particular migration. It requires a good chunk of development work and would certainly delay the overall migration to the Kubernetes platform.


### Timing Considerations of the Downtime-Inducing Approach

Testing has been performed in the stage environment to prove the approach works, and that was a pretty quick operation. However, the data in prod is much larger than stage's. More data means more time spent on all three migration operations: export, conversion, and import.

[A task](https://github.com/thunderbird/thundermail-deploy/issues/113) has been slated to do a test run of this, timing the operations so we can gauge how much downtime this will actually induce. Check that issue for updates on that information.
