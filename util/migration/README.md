# Cutover tooling

Scripts for the tb-stage cutover (platform-infrastructure milestone 1536). Every command
in the in-window issues sources the env file (tmpfs, gone on reboot: rebuild it) first,
then `util/migration/timing.sh`:

```bash
set -a; . /dev/shm/talos/cutover-tb-stage.env; set +a; . util/migration/timing.sh
```

## Conventions

- `check-prereqs.sh [--for KEY]` prints HAVE/NEED per tool and credential and exits with the
  NEED count (`--for T0` prints every row). It never prints a value.
- Operator values (`TEST_*`, `SPF_*_TXT`, `SUPPORT_CHANNEL`, `*_PR`) live in `~/.local/state/talos/cutover-operator.env`
  (mode 600, or both scripts stop); the builder and check-prereqs load its non-empty values, and the environment wins.
- `build-cutover-env.sh` writes the env file (mode 600, a `# source:` line above every
  variable, `NAME len=N` on stdout). Operator-supplied values (`TEST_*`, `SPF_*_TXT`, PR
  numbers) are read from the environment or carried over from the previous file; in-window
  appends (`FREEZE_SHA`, `FREIGHT`, ...) are carried over too. `--check` runs the read-only
  connectivity and snapshot/restore dry-run table and appends an `env-check` row to the TSV.
- Restore scripts are dry-run by default and print what they would change; `--apply`
  performs the writes. `--dry-run` is accepted and is the default. Snapshot scripts always
  write their file under `$EVIDENCE_DIR` (or `$SECRET_DIR` when the content is sensitive)
  and print the path as the last line.
- `timing.sh` defines `TS`, `t` (appends `STEP start end METRIC VALUE` to
  `$TIMING_TSV`), `pod` (exec in the runner's `postgres` container) and `waitfor`.
- Neon hosts are always the direct endpoints (`-pooler` stripped).

## Runner pod

`runner-pod.yaml` is a long-lived pod in `thundermail` carrying the Stalwart server
security group through the existing `stalwart-config-applier` SecurityGroupPolicy label
(not `app=stalwart`, so no Service selects it). It runs on `role: mail` nodes like Stalwart,
as uid 70 with no capabilities. The `postgres` container writes `/secrets/.pgpass` (a
memory volume) at start from the mounted Secrets, so no password is ever an exec argument,
and connects with `PGSSLMODE=verify-full` against the image's CA bundle. Every line names
its host; the pod refuses to start unless the three legacy hosts are distinct:

| line | matches | password |
|---|---|---|
| target host (direct and pooler form, read from the `stalwart-config` ConfigMap) | Stalwart target | `stalwart-postgresql-admin-password` |
| `rehearsal-legacy-pg` key `keycloak-host` | legacy Keycloak | key `keycloak` |
| `rehearsal-legacy-pg` key `accounts-host` | legacy Accounts | key `accounts` |
| `rehearsal-legacy-pg` key `stalwart-host` | legacy Stalwart | key `stalwart` |

The `python` container installs hash-pinned wheels as a non-root user and does not mount
`/work`.

`build-cutover-env.sh --check` runs `psql -Atc 'select 1'` against all four, which proves
the keying for the real hosts.

## Script notes

- `snapshot-zone.sh ZONE` keeps `data` for SRV/TLSA/CAA and also records a DoH answer per
  record as `zone-doh-before-<zone>-<ts>.txt`.
- `restore-zone.sh SNAPSHOT` creates missing records, updates changed ones and deletes
  records added since the snapshot; it never touches `_externaldns.*`, `*.dkim.*` or `*._domainkey.*`.
- `restore-ecs.sh` restores desiredCount first, then min/max, then clears the suspension.
- `snapshot-realm.sh URL` and `restore-realm.sh` use the Keycloak admin REST API through
  `curl` with the client credentials from the env file (same calls `kcadm` would make).
- `gate-django-migrations.sh --capture` (#1551, while `accounts-web` is up) saves the image's
  migration plan and image as `django-plan-<ts>.txt`/`.image` in the evidence dir. The gate
  compares that plan with `django_migrations` on the legacy main branch read through the
  runner and fails if the deployment image changed, so it works with Accounts scaled to 0.
- `gate-keycloak-version.sh` compares `migration_model` on the legacy main branch (runner)
  with `KC_IMAGE_VERSION`, or with `kc.sh --version` run by podman on the statefulset image;
  it never execs into a Keycloak pod.
- `dump-check.sh [principals.json]` compares the legacy server's principal total per type with
  the dump (`migrate_v016.py dump` skips failed reads and still exits 0); W4 runs it before
  `convert`. The `other` filter, which legacy ignores (it returns the overall total), and `tenant`
  unreadable on both sides are not compared; any other difference exits 1, and the matched
  per-type counts and the dump size must both equal the overall total. W4 accepts the dump log's
  community-edition `skipping principal type 'tenant'` (`Enterprise feature`) line and stops on
  any other skip or failed fetch. It and `--check` send the legacy admin password only to a
  loopback `OLD_STALWART_URL` owned by this user's session-manager-plugin.
- `restore-target-db.sh` refuses to run unless the target host equals the tb-stage overlay
  host (direct form), differs from the legacy host, and `current_database()` is `neondb`.
- `retarget-export.sh export.json` points the converted export at tb-stage: BlobStore becomes S3
  `$TARGET_BLOB_BUCKET` (region from the overlay `values.yaml`, keys `None`) and InMemoryStore
  becomes `{"@type": "Redis", "url": $REDIS_URL}`; the original is kept as `export.json.pre-retarget`.
- `quota-reset.sh --apply` creates the `resetUserQuotas` task from `quota-reset.json.tpl` for
  `$STALWART_ADMIN_ACCOUNT_ID`, waits up to `QUOTA_RESET_TIMEOUT` (600 s) until no StoreMaintenance
  or AccountMaintenance task is queued, then prints how many accounts show usage.
- `promotion.yaml.tpl` (tb-stalwart-configs) and `promotion-accounts.yaml.tpl` (tb-accounts) hold
  only `__TS__` and `__FREIGHT__`.
- `apply_unmigrated.py` re-applies the v0.15 settings `convert` leaves out; `--only KIND[,KIND]`
  is the inverse of `--skip` (`python3 test_apply_unmigrated.py`).
- `shapes.py` reads dump values the way `migrate_v016.py` does (wrapped lists and strings,
  durations, address domains); the helpers and `verify/` use it (`python3 test_shapes.py`).
- `app_password_users.py` and `totp_users.py` take `--principals principals.json --out FILE.csv`
  and list who loses app passwords or uses TOTP; `APP_PASSWORDS.md` has the notices and
  `export_contacts.py` the Accounts contact export. `verify/` is the read-only check suite.
- `migrate_v016.py` is the upstream release copy (stalwartlabs/stalwart v0.16.26,
  `resources/scripts/migrate_v016.py`, sha256 `ebc7c2cc8b3d9378476523ee5fd76c39b1bc0e788a890ff17b507b03c0757458`).
  Install its dependencies with `pip install -r util/migration/requirements.txt`.
- `thaw.sh` (run after the freeze PR merges with a merge commit) reverts the merged
  freeze commit on a branch from `main`, opens the draft thaw PR on platform-infrastructure
  and appends `FREEZE_SHA` and `THAW_PR` to the env file. Needs `FREEZE_PR` and `PI_DIR`.
  Dry run by default (prints the git and gh commands); `--apply` writes. It refuses to run
  until `FREEZE_PR` is merged with a merge commit.
- `post/p1-verify.sh` checks the Appointment stage reconnection: new OIDC and Accounts
  hosts in the frontend bundle, OIDC discovery and login page, API codes, and the CalDAV
  connect when `P1_OIDC_ACCESS_TOKEN` or `P1_CALDAV_USER`/`P1_CALDAV_APP_PASSWORD` are set; a skipped check makes it exit 2 (INCOMPLETE).
- `post/p3-verify.sh [--apply]` checks the `tbpro` reset-credentials form and an IMAP login to
  `TEST_MAILBOX`; with `--apply` it requests a reset for `TEST_USER`, waits for the mail
  (headers only) and fails unless `From` is on `auth-stage.tb.pro` and the DKIM `d=` or
  Return-Path is `amazonses.com`. Exit 3 = wrong `From`, 5 = not relayed by SES, 4 = no mail.
  Appends the `p3-smtp` timing row. `TEST_MAILBOX` must be `TEST_USER`'s Keycloak `email`
  (Accounts sets it to the backup address); Keycloak sends the reset there, so a mismatch
  shows up as exit 4, not as an SMTP fault.
- `snapshot-smtp.sh` prints `host`, `from`, `envelopeFrom` and `user` of the realm `smtpServer`.
- `post/p2-redirects.sh [ZONE_ID]` (default `CF_ZONE_ID_TBPRO`, must be tb.pro) puts the
  two 301 rules (`accounts-stage.tb.pro/*` and `auth-stage.tb.pro/*` to the same path on the
  stage-thundermail.com hosts) first in the tb.pro `http_request_dynamic_redirect` entrypoint,
  then sets the two records proxied. It writes only its own rules (POST, or PATCH when the ref
  exists, with `position.before ""`; PUT only when the entrypoint does not exist yet), so other
  rules are never rewritten. It saves the current entrypoint to
  `$EVIDENCE_DIR/tbpro-rules-before-*.json` and, with `--apply`, the records to
  `tbpro-p2-records-before-*.json`; it prints every call. Writes need `--apply`. It runs with the
  P2 token: `CF_API_TOKEN_AGENT` swapped for a tb.pro-only token with `expires_on`
  (`check-prereqs.sh --for P2` checks both), revoked after `post/p2-verify.sh` passes.
  Rollback is `post/p2-redirects.sh --rollback [--apply]`: it unproxies the saved records first
  (restoring their TTL), then deletes every entrypoint rule whose ref starts with `p2_`. After a
  successful rollback it moves that cycle's records files to `tbpro-p2-rolled-back-<ts>/`.
- `post/p2-verify.sh` expects `301 https://<target>/login/?p2=1` from each tb.pro host (DoH
  plus `curl --resolve`, `TRIES` attempts 10 s apart) and exits 1 otherwise.

## Window scripts

`window/w1.sh`, `t7.sh`, `t8.sh`, `w2-cut-legacy.sh`, `w3-blob-copy.sh`, `w4-migrate.sh`,
`w5-promote.sh`, `w6-verify.sh`, `w7.sh` (with `w7-verify.sh`) and `w8.sh` run one in-window step each. Each sources `$CUTOVER_ENV` (default
`/dev/shm/talos/cutover-tb-stage.env`, else `~/.local/state/talos/cutover-tb-stage.env`) itself, so values appended by an earlier step
(`CUT_ECS_END`, `STALWART_ADMIN_ACCOUNT_ID`, `T6_SHA`, `FREIGHT`) reach the next one.

- `--dry-run` (default) runs `check-prereqs.sh --for W<n>`, the snapshots and read-only checks,
  and prints every write as the exact command; it never runs `kubectl exec`. Timing rows get a
  `dryrun-` prefix. `--apply` runs the writes.
- W2 writes only with `--apply --go '<operator words>'`; W5 phase 1 (`--apply`) stops after
  the `apply_unmigrated.py` dry run and records the plan counts, and phase 2 (`--apply --go`)
  refuses a changed plan; W4 needs `--go` once a `drop-schema` row exists for the current cut.
  The go is recorded as a `go-W<n>` row in `$TIMING_TSV`.
- Legacy credentials are sent only to a loopback `OLD_STALWART_URL` served by the operator's own
  SSM tunnel, and the Stalwart credentials only to a `kubectl port-forward` owned by the operator.
- Output is logged to `$EVIDENCE_DIR/w<n>-*-<mode>-<ts>.log`. Any failed check or signal prints
  `STOP`, writes a `stop-W<n>` row, prints the pre-staged restore commands and exits 1. Nothing
  rolls back automatically. Exit 2 means a NEED item is listed.
- Restore files: `cut-legacy-restore.txt` for W2 and `w<n>-*-restore.txt` otherwise, written by
  `--apply` once per cut (a `restore-W<n>` row); dry runs write `*.dry-run.txt`.
- W3 accepts objects Stalwart wrote into the target bucket before the copy when none of them is
  in the legacy bucket (the 0.16 web-admin bundle, written at pod start): it records their keys
  as a baseline, stages `rclone delete --exclude-from <baseline keys>` so a restore keeps them,
  and its `rclone check` is `--one-way` (every legacy object present and equal on the target).
- W5 snapshots `BlockedIp AllowedIp Domain Security Http MtaQueueQuota Calendar AddressBook
  WebHook BlobStore InMemoryStore` plus the kinds they reference (`AcmeProvider Directory Tenant
  DnsServer Role`); the dry run resolves the same snapshot. `settings objects=` counts plan lines
  (one per kind). The restore upserts that snapshot, un-bans the nginx pod IPs and reloads; it
  does not delete BlockedIp entries created during W5. The snapshot leaves out secrets (the
  WebHook `signatureKey`, DnsServer credentials): put them into the file before a restore
  `apply`.
- W5 phase 2 un-bans the Running nginx pod IPs after `apply_unmigrated.py --apply`
  (`stalwart-cli delete BlockedIp --ids <id>`, id from `stalwart-cli query BlockedIp --where
  address=<ip> --fields id --json`), runs `stalwart-cli create action/ReloadSettings` and
  `action/ReloadBlockedIps`, then reads back: `apply_unmigrated.py` dry run 0 changes besides the
  un-banned nginx pod IPs, and no nginx pod IP in `BlockedIp`. A rerun after a later stop skips
  the apply once the cut has an `unmigrated-applied` row. Dry-run Stalwart reads need the
  operator's own port-forward.
- After the W2 revoke the security group has no ingress rules and a restore re-adds them with new
  ids, so rebuild the env file (`build-cutover-env.sh`) before any W2 rerun.
- W7 runs one GATE step per `--apply --go` (1 SPF edits, 2 merge PR 148, 3 flip and promote,
  4 `mail.` CNAME and `w7-verify.sh`); it records `FREIGHT_MANUAL` for the rollback promotion and
  edits `spf.` in place to `SPF_FINAL_TXT` (custom domains include it, so it is never deleted).
- Every in-window merge goes through `merge_pr` (window/lib.sh): `gh pr ready` on a draft, a stop when
  the branch still needs an approving review (collect approvals before the window), then
  `gh pr merge --merge`; an already merged PR passes.
- W4 also waits for the legacy task queue `f` to drain (`task-queue` row), compares account and
  group ids with `verify/02_directory.py` (`account-ids` row), and re-runs `rclone copy` and
  `check` after the W3 blob-check (`blob-recheck` row) before it leaves recovery mode. Convert
  stops on `warning: (dropping|renaming) (User|Group)` lines that are not in
  `$SECRET_DIR/convert-dropped-baseline.txt` (the lines the T10 dry run accepted).
- `window/w7-since-flip.sh` counts `message-ingest.ham|spam` (inbound) and
  `queue.authenticated-message-queued` (outbound) in the Stalwart logs since the `dns-cutover`
  start for a rollback decision. `window/test-sender-password.sh --apply` reads the app password
  created in the Accounts UI from stdin, checks a JMAP session and appends
  `TEST_SENDER_APP_PASSWORD`.
- `cf-record.sh ZONE_ID upsert|delete NAME TYPE [CONTENT]` is dry-run by default; an SPF upsert edits
  the one `v=spf1` TXT at NAME in place, empty content is refused, and it refuses to delete `spf.*`
  or touch `_externaldns.*`.
- `freight_for SHA` (window/lib.sh), used by W5 and W7, selects the Freight whose preview commit has the same
  `overlays/tb-stage-thundermail/stalwart-configs` and `bases/stalwart-configs` trees as SHA
  (preview commits never carry the merge SHA), and stops when TMD main has changed those paths since
  SHA or no Freight matches. `T6_SHA` comes from the env file or, when empty, from the merge commit
  of `T6_PR`, and W5 appends it. T7 and T8 read `T7_PR`/`T7_REVERT_PR` and `T8_PR`/`T8_REVERT_PR`.
