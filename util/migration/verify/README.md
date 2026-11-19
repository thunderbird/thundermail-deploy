# Stalwart 0.15 → 0.16 migration: verification checks

Read-only checks that compare a migrated Stalwart 0.16 server against its 0.15 source. Run them after the migration and before cutover. Each check prints `PASS`, `WARN` or `FAIL` lines and exits non-zero on any `FAIL`.

`window/w6-verify.sh` sources `<TARGET_ENV>.env` (`tb-stage.env` or `tb-prod.env`; variable names from the cutover env file, no values) and runs `run_all.sh` (flags: [In the window](#in-the-window)).

| Check | Compares |
|---|---|
| `01_source_snapshot.py` | Snapshots the source database: live documents per account and collection, every message's size and received time, event and contact index rows. Later checks compare against this. Prints the count of the 0.15 task queue (table `f`) and does not fail on it. `VERIFY_ACCOUNT_RANGE=lo-hi` reads only those accounts (a key range of `p`). |
| `02_directory.py` | Account ids, names, addresses, aliases, catch-all addresses, quota limits, domains, DKIM keys |
| `03_messages.py` | Every live message matched by account and document id, including messages the 0.15 search index missed. Size and received time compared against the source; every message is in some folder and thread. Downloads every migrated body (or a sample) and checks its size and headers. Messages only on the target whose subject starts with `w7-verify-` (gate check mail) are left out; other target-only messages are a WARN. `--sample` (in `sample.py`): no snapshot and no listing; see [In the window](#in-the-window). |
| `04_collections.py` | Per-account emails (gate check mail left out; every source message must still be on the target, matched by id, and only then is a higher count a WARN), folders, calendars, events, address books, contacts, files, Sieve scripts. Downloads file bodies. Matches event titles and contact emails by UID. |
| `05_folders.py` | Standard folders exist; each folder's total and unread counts agree with its messages |
| `06_buckets.py` | Source and target blob buckets: keys, sizes, objects the target has purged. The three listings go to sorted files in `VERIFY_OUT` (`bucket-source.tsv`, `bucket-target.tsv`, `bucket-target-delete-markers.txt`) and are compared line by line, so no listing is held in memory. |
| `07_target_config.py` | Right server (hostname), target bucket and stores, auto-banned internal addresses, blocked/allowed IPs, webhooks, quotas, ban rates, MTA-STS |

## Before the migration: who loses app passwords

Every v0.15 app password stops working on 0.16. `../app_password_users.py` lists the affected accounts from the v0.15 `principals.json`, and says which notice each one needs. Run it before the maintenance window. [APP_PASSWORDS.md](../APP_PASSWORDS.md) has the steps and both notice texts.

## Read-only

- **Source:** only `SELECT`s, and every session runs with `default_transaction_read_only=on`, so Postgres rejects any write. On tb-prod the source is branch `tb-prod-cutover-source`, read only through `STALWART_SOURCE_PG_HOST`, `STALWART_SOURCE_PG_DB`, `STALWART_SOURCE_PG_USER` and `STALWART_SOURCE_PG_PASSWORD`, which the reset phase of `window/w4-migrate.sh` writes to the cutover env file. With `TARGET_ENV=tb-prod` the suite refuses to start when one of them is empty, when `SOURCE_PG_SECRET_ID` or any `SOURCE_PG_*` name is set, or when the host names the endpoint of `production` (`STALWART_LEGACY_PG_HOST`; the password check against `STALWART_LEGACY_PG_PASSWORD` applies only where that name is set, which the tb-prod env file no longer holds). The body sample grows its `TABLESAMPLE` to at most 10% of `s_email` (all of it below 200,000 rows); messages without an `s_email` row are not sampled. `tb-prod.env` sets `SOURCE_PG_SECRET_ID` empty.
- **Target:** only JMAP `get`, `query` and blob downloads. Side effects: 0.16 creates default folders, a default calendar and a default address book the first time an account with none is opened. Check 04 counts these as added defaults. `Identity/get` is not called, because on 0.16 it creates missing identities and deletes ones whose address no longer belongs to the account. Run `apply_unmigrated.py` and the Reload actions before these checks.
- **Buckets:** only `list-objects-v2` and `list-object-versions`.

## Setup

Python 3.10+, the AWS CLI, and either `psql` (set `PSQL_MODE=local`) or `podman` (runs `psql` from `postgres:18-alpine`).

```bash
# target: the 0.16 admin API, e.g. through a port-forward to the Stalwart pod (not the Service)
export TARGET_URL=http://localhost:18081 TARGET_USER=<admin> TARGET_PASSWORD=<admin-password>

# AWS profile and region for the source (always needed: 06 lists the source bucket)
export SOURCE_AWS_PROFILE=<source-profile> AWS_REGION=<region>
# source database (tb-stage; tb-prod reads STALWART_SOURCE_PG_* only): a Secrets Manager secret with host/database/user/password ...
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
- `run_all.sh --group source` runs 01 only (the read-only legacy run before the window), `--group data` runs 01 to 06, `--group config` runs 07 and `--group compare` runs 02, `03 --sample` and 07 with no 01. `--piece buckets|counts|bodies [--resume]` runs one piece of the full compare (`pieces.py`). Each check's output is kept as `check-<name>.txt` in `VERIFY_OUT`.
- WARN lines that match no line of `expected-warn.txt` are repeated as `UNEXPECTED WARN` and counted in the last line.
- 01 prints the 0.15 task queue count (table `f`) and does not fail on it: documents are counted in `p`, and messages the index has not caught up with are compared by id only. `production` holds rows there; the 0.16 upgrade empties the table on the cutover branch, not on `tb-prod-cutover-source`.
- Run 01 from the source as close to cutover as possible. New mail on the source after the copy shows up as "missing on target".
- `VERIFY_SKIP_BODIES=1` downloads a random sample in 03 (`VERIFY_BODY_SAMPLE`, default 1% and at least 200) instead of every body. 06 then says that blob presence rests on the bucket comparison. An unset, empty or `0` `VERIFY_BODY_SAMPLE` gives the default; any other value that is not a whole number stops 03 with a message.
- Queries are paged, so the 0.16 limit of 5,000 results per query does not cut results short.
- `TARGET_INSECURE=1` turns off TLS verification for the target URL.

## In the window

`window/w6-verify.sh` takes one of these flags (with `--dry-run` or `--apply`):

| Flag | Runs | Timing row |
|---|---|---|
| none | `run_all.sh` (all checks), then the IMAP and JMAP folder count | `verify`, `imap-jmap-folders` |
| `--compare-only` | `run_all.sh --group compare`: 02 (directory, account and group ids equal to the dump), `03_messages.py --sample` and 07 (settings). No 01, 04, 05 or 06 and no folder count. | `verify-compare` |
| `--folders-only` | the IMAP and JMAP folder count alone | `imap-jmap-folders` |
| `--piece buckets\|counts\|bodies [--resume]` | one piece of the full compare; the dry run prints the command and does not run it | `verify-<piece>` |

`03_messages.py --sample` picks `VERIFY_BODY_SAMPLE` messages (default 200) on the source first: block samples of `s_email` (`TABLESAMPLE SYSTEM`, sized from `pg_class.reltuples` for 20 rows per wanted message and grown fourfold until enough are found), each row kept only when its email document is live in `p` (a primary key lookup). It then fetches only those on the target: one `Email/query` per message with a `receivedAt` window of the source time minus 2 s to plus 3 s, and that account's id list only when the window finds nothing. It compares size, received time, folder and thread, and downloads each body. Its bodies line reads `bodies downloaded (random sample): N/N of N sampled on the source`. Messages missing from `s_email` are never sampled.

The full compare runs as three pieces, buckets first:

| Piece | Checks | Unit | Expected time on tb-prod (estimates, not measured) |
|---|---|---|---|
| `buckets` | 06 with `VERIFY_PIECE=buckets`: blob references are left to piece `bodies` | one run | 15 to 45 min: three listings of about 5.85 million keys at 1,000 keys a request, in parallel, then a sort; one run |
| `counts` | 01, 04, 05 | batches of `VERIFY_BATCH` accounts (default 100), 51 batches for 5,020 accounts | tb-stage ran the whole suite in 810 s for 160 accounts (about 5 s an account), so several hours for 5,020 before mail volume: two or more runs |
| `bodies` | 01, then 03 with every body | the same batches | longer than `counts`: every body passes through the port-forward; several runs |

The resume key is `$EVIDENCE_DIR/verify-pieces/<piece>/progress.tsv` (`VERIFY_PIECES_DIR`; `run_all.sh` alone uses `$VERIFY_OUT/pieces`), one row per finished batch: `lo-hi`, `pass` or `fail`, the number of checks that failed, the start time and the seconds taken. `batches.txt` beside it fixes the account ranges at the first run. `--resume` skips the batches whose last row is `pass` and reruns the others; a run without `--resume` moves an earlier piece directory aside. No new batch starts once the time used plus the longest batch so far would pass `VERIFY_PIECE_BUDGET` seconds (default 12600, 3.5 h), so a run ends inside one 4-hour AWS session unless one batch takes longer. `buckets --resume` keeps listing files that are complete. Each run ends with `== piece <name>: N batches; passed P, failed F [...], left L (resume key <path>)`; W6 prints that line and reports `NEED  piece <name>: L batch(es) left` (exit 2) until `L` is 0.

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
