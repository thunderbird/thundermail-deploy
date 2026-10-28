# Stalwart 0.15 → 0.16 migration: verification checks

Read-only checks that compare a migrated Stalwart 0.16 server against its 0.15 source. Run them after the migration and before cutover. Each check prints `PASS`, `WARN` or `FAIL` lines and exits non-zero on any `FAIL`.

For tb-stage, `window/w6-verify.sh` sources `stage.env` (variable names from the cutover env file, no values) and runs `run_all.sh`.

| Check | Compares |
|---|---|
| `01_source_snapshot.py` | Snapshots the source database: live documents per account and collection, every message's size and received time, event and contact index rows. Later checks compare against this. |
| `02_directory.py` | Account ids, names, addresses, aliases, catch-all addresses, quota limits, domains, DKIM keys |
| `03_messages.py` | Every live message matched by account and document id, including messages the 0.15 search index missed. Size and received time compared against the source; every message is in some folder and thread. Downloads every body (or a sample) and checks its size and headers. |
| `04_collections.py` | Per-account emails, folders, calendars, events, address books, contacts, files, Sieve scripts. Downloads file bodies. Matches event titles and contact emails by UID. |
| `05_folders.py` | Standard folders exist; each folder's total and unread counts agree with its messages |
| `06_buckets.py` | Source and target blob buckets: keys, sizes, objects the target has purged |
| `07_target_config.py` | Right server (hostname), target bucket and stores, auto-banned internal addresses, blocked/allowed IPs, webhooks, quotas, ban rates, MTA-STS |

## Before the migration: who loses app passwords

Every v0.15 app password stops working on 0.16. `../app_password_users.py` lists the affected accounts from the v0.15 `principals.json`, and says which notice each one needs. Run it before the maintenance window. [APP_PASSWORDS.md](../APP_PASSWORDS.md) has the steps and both notice texts.

## Read-only

- **Source:** only `SELECT`s, and every session runs with `default_transaction_read_only=on`, so Postgres rejects any write.
- **Target:** only JMAP `get`, `query` and blob downloads. Side effects: 0.16 creates default folders, a default calendar and a default address book the first time an account with none is opened. Check 04 counts these as added defaults. `Identity/get` is not called, because on 0.16 it creates missing identities and deletes ones whose address no longer belongs to the account. Run `apply_unmigrated.py` and the Reload actions before these checks.
- **Buckets:** only `list-objects-v2` and `list-object-versions`.

## Setup

Python 3.10+, the AWS CLI, and either `psql` (set `PSQL_MODE=local`) or `podman` (runs `psql` from `postgres:18-alpine`).

```bash
# target: the 0.16 admin API, e.g. through a port-forward to the Stalwart pod (not the Service)
export TARGET_URL=http://localhost:18081 TARGET_USER=<admin> TARGET_PASSWORD=<admin-password>

# AWS profile and region for the source (always needed: 06 lists the source bucket)
export SOURCE_AWS_PROFILE=<source-profile> AWS_REGION=<region>
# source database: either a Secrets Manager secret with host/database/user/password ...
export SOURCE_PG_SECRET_ID=<secret-id>
# ... or the values directly
# export SOURCE_PG_HOST=<direct-endpoint> SOURCE_PG_DB=<db> SOURCE_PG_USER=<user> SOURCE_PG_PASSWORD=<password>

# principals.json from `migrate_v016.py dump`
export PRINCIPALS=principals.json

# buckets (06) and expectations (07, all three required)
export SOURCE_BUCKET=<source-bucket> TARGET_BUCKET=<target-bucket> TARGET_AWS_PROFILE=<target-profile>
export EXPECT_BUCKET=<target-bucket> INTERNAL_CIDRS=<cluster-cidr> EXPECT_HOSTNAME=<mail-hostname>

export VERIFY_OUT=out   # where snapshots go (created mode 700)
./run_all.sh
```

- `run_all.sh` deletes old snapshots, runs 01, and stops if 01 fails. 03 and 04 read the snapshot 01 writes; 06 reads the blob list written by 03.
- `run_all.sh --group source` runs 01 only (the read-only legacy run before the window), `--group data` runs 01 to 06 and `--group config` runs 07. Each check's output is kept as `check-<name>.txt` in `VERIFY_OUT`.
- WARN lines that match no line of `expected-warn.txt` are repeated as `UNEXPECTED WARN` and counted in the last line.
- 01 fails if the 0.15 task queue (table `f`) is not empty. Pending index updates mean the snapshot would be incomplete; let them drain first.
- Run 01 from the source as close to cutover as possible. New mail on the source after the copy shows up as "missing on target".
- `VERIFY_SKIP_BODIES=1` downloads a random sample in 03 (`VERIFY_BODY_SAMPLE`, default 1% and at least 200) instead of every body. 06 then says that blob presence rests on the bucket comparison.
- Queries are paged, so the 0.16 limit of 5,000 results per query does not cut results short.
- `TARGET_INSECURE=1` turns off TLS verification for the target URL.

## Reading the results

A clean tb-dev rehearsal (4,163 messages, 160 accounts) gives `FAIL` only for what the migration actually loses, and `WARN` for expected differences:

| Line | Meaning |
|---|---|
| `FAIL catch-all addresses missing` | `convert` drops `@domain` catch-alls. Set `Domain.catchAllAddress`. |
| `FAIL auto-banned internal addresses` | 0.16 banned a proxy or load balancer address. Remove the ban. Allow only the ingress pod range, and only after confirming Stalwart sees real client IPs (PROXY protocol). |
| `WARN mailboxes ... got the default set` | Accounts with no folders on the source get the default folders. |
| `PASS only on source: N (N purged by the target ...; 0 missing ...)` | 0.16 deletes blobs no document references. With bucket versioning they can be restored. Any object missing with no delete marker is a `FAIL`. |
| `WARN MTA-STS MX hosts: []` | The MX host isn't set yet. |

Everything else should be `PASS`.

## How the source counts work

The 0.15 Postgres store keeps each document's properties in table `p`, with key = account (u32, big-endian) + collection (u8) + field (u8) + document id (u32, big-endian). Field 50 appears exactly once per live document, so counting it per collection gives the real number of messages, folders, events and so on. The search index tables (`s_email`, `s_cal`, `s_card`) are not a reliable count: they keep rows for deleted items and miss some live ones. They're only used here to join message size and received time, and event and contact text, onto live documents.

Collections: 0 email, 1 mailbox, 3 identity, 5 Sieve script, 8 calendar, 9 event, 10 address book, 11 contact, 12 file.
