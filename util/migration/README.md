# Cutover tooling

Scripts for the tb-stage cutover (platform-infrastructure milestone 1536) and the tb-prod
cutover (milestone 1719). Every shell script needs `TARGET_ENV` set to `tb-stage` or `tb-prod` and
exits 64 without it. `targets/$TARGET_ENV.sh` holds the names for that environment (cluster,
profiles, overlays, Argo apps, Kargo stages, zones, legacy secret paths and filters); ids, hosts
and secrets are looked up at build time. A `REPLACE_ME_*` value (any case) is one a later change
fills in; while one is left in `targets/$TARGET_ENV.sh` or `verify/$TARGET_ENV.env`, every `--apply` exits 64.
Every script loads the env file itself at start (`$CUTOVER_ENV`, default
`/dev/shm/talos/cutover-$TARGET_ENV.env`; tmpfs, gone on reboot: rebuild it), and its values win over
the shell's; the builder is the exception. A shell command that reads a variable sources the file again first:

```bash
export TARGET_ENV=tb-prod
set -a; . "/dev/shm/talos/cutover-$TARGET_ENV.env"; set +a; . util/migration/timing.sh
```

## Conventions

- `check-prereqs.sh [--for ID[,ID...]]` prints HAVE/NEED per tool and credential and exits with
  the NEED count; without `--for` it prints every row. ID is a gate id (`go-W1` ... `go-rollback`,
  `start`, `checks`, `presync`, `backups`, `verify`, `auth-dns`, `pre-cut`); a list prints one
  block per id. `start` holds only the rows that can pass before the first window step. It reads
  values from the env file first, then the environment, and never prints a value.
- `kubectl` is a shell function after any script sources `target.sh`: it exits 64 when a call
  other than `kubectl kustomize` has no `--kubeconfig`, when the value is empty, names a missing file or names the other environment's cluster,
  or when a flag picks another cluster or credential (`--context`, `--cluster`, `--server`, `--user`,
  `--token`, `--as`, the client certificate and TLS flags), so no script falls back to the current context. On tb-prod the current-context of `KUBECONFIG_TARGET` must be
  cluster `mzla-eks-tb-prod01`. `KUBECONFIG_TARGET` replaces `KUBECONFIG_STAGE`; with `TARGET_ENV=tb-stage` the old
  name is still read.
- Operator values (`TEST_*`, `SPF_*_TXT`, `SUPPORT_CHANNEL`, `*_PR`, `ADMIN_MERGE`) live in `~/.local/state/talos/cutover-operator-$TARGET_ENV.env`
  (mode 600, or both scripts stop; tb-stage falls back to `cutover-operator.env`; a tb-prod file needs a `TARGET_ENV=tb-prod` line); the builder and check-prereqs load its non-empty values, and the environment wins.
- `build-cutover-env.sh` writes the env file (mode 600, a `# source:` line above every
  variable, `NAME len=N` on stdout). Operator-supplied values (`TEST_*`, `SPF_*_TXT`, PR
  numbers) are read from the environment or carried over from the previous file; in-window
  appends (`FREEZE_SHA`, `FREIGHT`, ...) are carried over too. `--check` runs the read-only
  connectivity and snapshot/restore dry-run table and appends an `env-check` row to the TSV; it
  moves its own snapshots into `env-check/` so rollback never picks them. On tb-prod it prints
  `WAIT` (not a failure) for rows whose object only exists in the window (runner pod, SSM tunnel,
  Stalwart, the mail load balancer) and skips the tb-stage-only rows.
- The tb-prod build writes `TMD_CHECKOUT` (the checkout it runs from), `WINDOW_SHA` (its `HEAD`;
  `--rebuild` keeps the recorded value) and `STALWART_URL=http://localhost:18081`, as tb-stage
  does; `LANE_PF_PORT` replaces the port. After a tooling fix is checked out in the window,
  `build-cutover-env.sh --rebuild --move-window-sha` sets `WINDOW_SHA` to the new `HEAD`. Before the
  mail load balancer exists `TARGET_NLB_DNS` is empty; `build-cutover-env.sh --rebuild` fills it
  once it does. `TBPROD_AUTH_ALB` and `TBPROD_ACCOUNTS_ALB` come from the Ingress with a rule
  for `AUTH_HOST` or `ACCOUNTS_HOST`.
- The Cloudflare token is `CF_API_TOKEN_AGENT` from `CF_TOKEN_FILE` (`~/.prod-cloudflare.env` for
  tb-prod, `~/.cloudflare.env` for tb-stage).
- Restore scripts are dry-run by default and print what they would change; `--apply`
  performs the writes. `--dry-run` is accepted and is the default. Snapshot scripts always
  write their file under `$EVIDENCE_DIR` (or `$SECRET_DIR` when the content is sensitive)
  and print the path as the last line.
- `timing.sh` defines `TS`, `t` (appends `STEP start end METRIC VALUE` to
  `$TIMING_TSV`), `pod` (exec in the runner's `postgres` container) and `waitfor`.
- Neon hosts are always the direct endpoints (`-pooler` stripped).

## Runner pod

`runner-pod.sh` prints `runner-pod.yaml` for `TARGET_ENV` (label, `/work` size and the legacy
Secret name from `targets/$TARGET_ENV.sh`); `runner-pod.sh --secret` writes that Secret under
`$SECRET_DIR` and prints its path. `runner-pod.yaml` is a long-lived pod in `thundermail` carrying the Stalwart server
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

On tb-prod, `RUNNER_LEGACY_PG_KEYS='keycloak accounts'` in `targets/tb-prod.sh` drops the
legacy Stalwart line, and `runner-pod.sh` refuses a tb-prod key list that names `stalwart`. The
Secret (still named by `LEGACY_PG_SECRET_NAME`) holds only the Keycloak and Accounts keys and
hosts, and neither host may equal `STALWART_LEGACY_PG_HOST`. The pod wants two distinct legacy
hosts and refuses to start when the mounted Secret has a `stalwart` key. `.pgpass` holds the
target, Keycloak and Accounts lines. `/work` keeps its 20Gi default, since `RUNNER_WORK_SIZE` is
unset. `--check` skips the legacy Stalwart database rows. On tb-prod W4 runs neither `task-queue`
nor `db-dump-legacy` (they refuse if reached); its reset phase works on the cutover branch.

The `python` container installs hash-pinned wheels as a non-root user and does not mount
`/work`.

`build-cutover-env.sh --check` runs `psql -Atc 'select 1'` against all four, which proves
the keying for the real hosts.

## Script notes

- `snapshot-zone.sh ZONE` keeps `data` for SRV/TLSA/CAA and also records a DoH answer per
  record as `zone-doh-before-<zone>-<ts>.txt`; when a lookup fails it prints
  `doh capture partial: N of M names unread`.
- `body-update.py --tick <gate> --evidence <file>` and `--close <PR URL>` edit issue bodies;
  `--dry-run` prints the diff and the body size and writes nothing (`--help` has the formats).
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
  (the `cf-token-agent-redirects` row of `check-prereqs.sh` checks both), revoked after `post/p2-verify.sh` passes.
  Rollback is `post/p2-redirects.sh --rollback [--apply]`: it unproxies the saved records first
  (restoring their TTL), then deletes every entrypoint rule whose ref starts with `p2_`. After a
  successful rollback it moves that cycle's records files to `tbpro-p2-rolled-back-<ts>/`.
- `post/p2-verify.sh` expects `301 https://<target>/login/?p2=1` from each tb.pro host (DoH
  plus `curl --resolve`, `TRIES` attempts 10 s apart) and exits 1 otherwise.

## Window scripts

`window/w1.sh`, `t7.sh`, `t8.sh`, `w2-cut-legacy.sh`, `w3-blob-copy.sh`, `w4-migrate.sh`,
`w5-promote.sh`, `w6-verify.sh`, `w7.sh` (with `w7-verify.sh`) and `w8.sh` run one in-window step each. Each sources `$CUTOVER_ENV` (default
`/dev/shm/talos/cutover-$TARGET_ENV.env`, else `~/.local/state/talos/cutover-$TARGET_ENV.env`) itself, so values appended by an earlier step
(`CUT_ECS_END`, `STALWART_ADMIN_ACCOUNT_ID`, `T6_SHA`, `FREIGHT`) reach the next one.

- `--dry-run` (default) runs `check-prereqs.sh --for <gate id>` (`GATE_ID`, else the script's
  go row), the snapshots and read-only checks,
  and prints every write as the exact command; it never runs `kubectl exec`. Timing rows get a
  `dryrun-` prefix. `--apply` runs the writes.
- W2 writes only with `--apply --go '<operator words>'`; W5 phase 1 (`--apply`) stops after
  the `apply_unmigrated.py` dry run and records the plan counts, and phase 2 (`--apply --go`)
  refuses a changed plan; W4 needs `--go` once a `drop-schema` row exists for the current cut.
  The go is recorded as a `go-W<n>` row in `$TIMING_TSV`.
- Legacy credentials are sent only to a loopback `OLD_STALWART_URL` served by the operator's own
  SSM tunnel, and the Stalwart credentials only to a `kubectl port-forward` owned by the operator
  (on tb-prod, one started with `--kubeconfig $KUBECONFIG_TARGET`).
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
- W5 `accounts-stalwart-check` needs the #1539 Accounts bundle write, which happens outside the
  script. While the cut has no `put-accounts-bundle` row in `$TIMING_TSV`, a failed check prints
  `PENDING` and W5 exits 2 instead of stopping; write the bundle, record that row, and rerun
  `--apply --go`. With the row present, a failed check is a STOP. Record the row with
  `printf 'put-accounts-bundle\t%s\t%s\t\t\n' "$(date -u +%FT%TZ)" "$(date -u +%FT%TZ)" >> "$TIMING_TSV"`
  (the PENDING line prints it with the path filled in). A 200 without the admin account is
  recorded as `200-no-account`. W7 `--apply` stops unless the latest `accounts-stalwart-check`
  value for the cut is 200.
- After every stalwart-configs promote (W5 phase 1 `settings-promote`, W7 step 3) the script
  restarts Stalwart (`restart_stalwart` in window/lib.sh: `rollout restart deploy/stalwart`, then
  `rollout status`), because 0.16 reloads certificates but does not rebind its listeners after
  the config Job. It then port-forwards a Running `stalwart-nginx` pod (local 10025, 10465, 10587, 10993; it
  refuses to start when one is already in use and waits until kubectl owns all four) and
  needs TLS verified for `mail.stage-thundermail.com` on 25, 465, 587 and 993 (STARTTLS on 25 and
  587) within 60 s per port, else the step stops. This works from the operator host without port
  25 egress. An open Stalwart port-forward is reopened on the new pod. The W5 restore file ends with
  the same restart.
- After the W2 revoke the security group has no ingress rules and a restore re-adds them with new
  ids, so rebuild the env file (`build-cutover-env.sh`) before any W2 rerun.
- W7 runs one GATE step per `--apply --go` (1 SPF edits, 2 merge PR 148, 3 flip and promote,
  4 `mail.` CNAME, then the apex `/.well-known/{caldav,carddav,jmap,autoconfig}` 302 to
  `mail.stage-thundermail.com` as a zone dynamic redirect rule (the origin behind the apex CNAME
  302s them to production; rule snapshot plus `restore-redirect.sh` line R in the restore file),
  and `w7-verify.sh`); it records `FREIGHT_MANUAL` for the rollback promotion and
  edits `spf.` in place to `SPF_FINAL_TXT` with TTL 300 in one upsert (custom domains include it,
  so it is never deleted); the apex and `mail.` SPF TXTs stay as they are (#1535).
- W6 runs `imap-jmap-folders` even when `verify` fails, then stops with the verify result.
- `w7-verify.sh` prints one PASS, FAIL or NEED row per check. NEED (for example no port 25
  egress from this host) counts as not passed: it fails the gate like FAIL, and the summary and
  the STOP line read `passed=N not passed=M (FAIL=a NEED=b)`. Run it from a host that reaches
  port 25 on the new NLB. The `smtp-25-starttls` probe needs EHLO on 25 to list STARTTLS and
  the certificate to verify for `mail.stage-thundermail.com`; the inbound sends use `swaks --tls
  --tls-verify` and fail on a server without STARTTLS. They send from `INBOUND_FROM` (operator env file, carried by
  `build-cutover-env.sh`), an address in a domain whose SPF passes for the sending host; it has no
  default, and an empty value or an address in the domain of the zone, `TEST_MAILBOX` or `TEST_USER`
  (any case) stops W7 before any mail is sent. `first-delivery` passes only
  when the message is in `TEST_USER`'s Inbox and its top `Received` header shows TLS (`with
  ESMTPS`).
- W8 disables thunderbird-accounts `merge.yml` (deploy-stage) after the go and before the thaw,
  cancels its unfinished runs (`accounts-merge-yml` row), and at the end checks `disabled_manually`,
  no unfinished runs and legacy ECS `desiredCount` 0; re-enable it only after the
  thunderbird-accounts PR that sets the four stage `desired_count` to 0 merges.
- Every in-window merge goes through `merge_pr` (window/lib.sh): `gh pr ready` on a draft, a stop when
  the branch still needs an approving review, then `gh pr merge --merge --match-head-commit`; an already
  merged PR passes. With `ADMIN_MERGE=1` (operator decision 2026-10-28) a `REVIEW_REQUIRED` PR merges
  with `--admin` once its checks are green and Greptile's latest comment scores 5/5 on the head
  (`admin-merge` row). The dry run reads the PR and reports the merge or the refusal as a NEED.
- W4 also waits for the legacy task queue `f` to drain (`task-queue` row), compares account and
  group ids with `verify/02_directory.py` (`account-ids` row), and re-runs `rclone copy` and
  `check` after the W3 blob-check (`blob-recheck` row) before it leaves recovery mode. Convert
  stops on `warning: (dropping|renaming) (User|Group)` lines that are not in
  `$SECRET_DIR/convert-dropped-baseline.txt` (the lines the T10 dry run accepted).
- On tb-prod W4 has three phases, and `--apply` without one exits 64; the dry run without a
  phase prints all three. `--phase read` (gate `go-W4-read`) dumps through `dump_legacy.py` (the
  password stays in the environment, `DUMP_WORKERS` concurrent reads, default 8), then runs
  `duplicates`, `convert`, `domains`, `retarget` and `queue-drain` last. `domains` runs
  `domain_fix.py` on the export. It drops each Domain that Stalwart 0.16 refuses, with the DKIM
  signatures that point at it: a name no public suffix rule matches (`domain-suffixes.txt`, the
  rules of the `psl` crate 2.1.241 that Stalwart 0.16.27 is built with, in ASCII form, plus the
  reserved names 0.16 accepts), and both entries of a name present in Unicode and in punycode
  (compared after NFKC and punycode, as 0.16 reads them). It prints one
  `warning:` line per dropped Domain and DkimSignature, and the step reports their count without
  stopping. A dropped Domain that an account, list or setting of the export still uses stops it, and so does an address of `principals.json` on a dropped Domain. A catch-all there (`@<domain>`, which `convert` leaves out and W5 puts back) is a `warning:` line instead: `apply_unmigrated.py --dropped-domains` (W5 on tb-prod) skips it and prints the count. `domain_fix.py --dropped-list` writes the list that `verify/02_directory.py` reads (`DROPPED_DOMAINS`). Names are compared in the punycode form 0.16 stores (`shapes.norm_domain`).
  `dup_check.py` reports the same two kinds as `class Domain:idna-duplicate` and
  `class Domain:refused-by-0.16` lines, and the names convert drops itself as
  `class Domain:dropped-by-convert`, without changing its exit code. It writes `$SECRET_DIR` and the
  `w4-export` and `w4-read-done` rows only. A rerun after a `queue-drain` stop reads the queue
  only; `--go` dumps again. `--phase reset` (gate `go-W4-reset`, no legacy admin API) refuses
  without a `w4-read-done` row and a later `stop-legacy-stalwart` row after the last `cut-ecs`
  row, or while a Stalwart pod exists. It then runs `neon_branch.py`: it resets
  `tb-prod-cutover` from its parent and proves the reset by `last_reset_at`, later than the stop
  row. It replaces `tb-prod-cutover-source` with a read-only branch whose own password goes into
  `STALWART_SOURCE_PG_*` of the env file, created at the `parent_lsn` the reset returned (row
  `reset-lsn`), so a `production` that moves between the two calls does not stop a good reset;
  the reset proof fails when `parent_lsn` did not change. Neon resets a role password only on a
  branch with a read-write endpoint, so the branch is created with one, the password is reset, a
  read-only endpoint is added and the read-write endpoint is deleted. It then checks that both branches are
  `ready`, later than the stop row and at the same `parent_lsn`. It also reads schema `00000005`, runs the guard (both
  credentials log in on their branch, the source branch refuses a write as read-only, and the
  production endpoint answers `password authentication failed` to each), and runs the pre-delete in one transaction (`DELETE <n>`
  equal to the count before, then `0`). Its rows are `reset` and `pre-delete`. It ends with the
  boot steps: it writes a `recovery-wait` row and waits up to `RECOVERY_WAIT` (3600 s) for one
  recovery-mode pod started after the pre-delete, then 900 s for `STALWART_SCHEMA_VERSION`, and
  applies the export. A second reset needs `--go`. `--phase boot` resumes an expired wait
  without a second reset. tb-prod runs no `task-queue`, dump of a database, `DROP SCHEMA`,
  restore or blob step.
- `window/legacy-stalwart.sh restart|stop|start|read [--dry-run | --apply]` (tb-prod only, dry run
  by default) sends one SSM Run Command (`AWS-RunShellScript`) to the nodes of
  `LEGACY_STALWART_NODE_IDS`, after it checks that there are six distinct ids, an `mzla-legacy`
  session, and that each node is `running`, `Online` in SSM, named `mailstrom-prod-stalwart-*` and
  tagged `postboot.stalwart.node_services`. `restart` (W 2.3) runs `systemctl restart thundermail`
  on the four mail nodes and refuses after a `stop-legacy-stalwart` row of this cut. `stop` (W 4.3)
  runs `systemctl disable --now thundermail` on all six and refuses unless `window/gate.sh
  stop-legacy` passes. `start` (Rollback) writes a `start-legacy-stalwart` row, which voids an
  earlier stop row for the W4 reset, then runs `systemctl enable --now thundermail; sleep 5;
  systemctl is-active thundermail` and passes only when every node prints `active`. It also sends
  to the reachable nodes when some are not `running` or not `Online`, then exits 1 naming the rest;
  the other modes refuse unless all six are reachable. `read` (W 4.3 after the stop, W 6.4,
  Rollback) asks for `systemctl is-active thundermail; systemctl is-enabled thundermail`, prints one
  `node <name> <id> <Status> rc=<code> <active> <enabled>` line each and a `summary:`, and writes
  the `stop-legacy-stalwart` row only when `window/gate.sh stop-legacy` passes and all six report
  `inactive` (or `failed`) and `disabled`. The reset phase and `blob_compare.py` read that row.
  Exit codes: 0 done (for `read`: all six stopped, or all six `active` and `enabled`), 1 `STOP`
  (a node failed, did not answer within `LEGACY_SSM_WAIT`, or `start` could not reach a node), 2
  `NEED` (nothing sent), 3 `read` found mixed states, 64 usage.
- On tb-prod a step that fails writes no timing row, so every row a gate reads means a pass:
  `queue-drain` (value 0), `stop-legacy-stalwart`, `reset`, `pre-delete`, `recovery-wait`,
  `verify-compare` (metrics `fails` and `unexpected_warns`). A stop writes `stop-W4` or `stop-W6`.
  A failed W6 suite stops at once on tb-prod. `window/gate.sh stop-legacy | merge-recovery |
  verify-compare` reads them after the last `cut-ecs` row and prints `GO    <gate>: ...` (exit 0)
  or `WAIT  <gate>: <reason>` (exit 2): `stop-legacy` wants a `queue-drain` 0 row before a
  `w4-read-done` row and no later `w4-read-start` (every `--phase read --apply` writes one, so a
  failed rerun of the read phase holds the gate); `merge-recovery` wants `recovery-wait` as the last of
  `reset`, `pre-delete`, `recovery-wait`, `stop-W4` (a `--phase boot` resume writes it again);
  `verify-compare` wants both rows at 0 and no later `stop-W6`.
- On tb-prod `$SECRET_DIR` is on disk, not tmpfs: `~/.local/state/talos/cutover-tb-prod-secret/attempt-<n>`
  (mode 700, files 600 under umask 077), so a reboot loses none of `export.json`,
  `principals.json`, `settings.json`, `domains-dropped.json`, `domain-fixed.txt`,
  `duplicates.txt`, `source-branch.env` or `apply.log`, and `--rebuild` finds them where they are
  (it takes `STALWART_SOURCE_PG_*` from `source-branch.env`). Every script refuses a secret
  directory, or a file in it, that is a symlink, not the operator's, or open to group or others.
  W8 prints the delete for after #1577 signs off: `rm -rf ~/.local/state/talos/cutover-tb-prod-secret <env file>`.
- `verify/02_directory.py` reads `DROPPED_DOMAINS` (`$SECRET_DIR/domains-dropped.json`, written by
  `domain_fix.py --dropped-list`): it leaves exactly those Domains out of the source set, prints
  their count, and compares the DkimSignature count of tb-prod with the export after the drop
  (fewer is a failure, more a warning on the expected list).
- On tb-prod `window/w5-promote.sh` takes `T6_SHA` from the merge commit of PR 195
  (`T6_PR_DEFAULT` in `targets/tb-prod.sh`) when the env file has neither `T6_SHA` nor `T6_PR`.
- `window/freight-for.sh <commit on main>` prints the newest Freight of `KARGO_WAREHOUSE_CONFIGS`
  whose preview commit holds the settings trees of that commit (`Freight <name> (preview commit
  <sha>) matches <commit>`), the same match `w5-promote.sh` uses; the Warehouse follows the
  preview branch, so a merge commit on `main` is never a Freight commit itself.
- `gate-keycloak-version.sh --pull[=IMAGE]` runs the image once before the window and prints
  `image <ref> cached: Keycloak <version>`; the gate prints the podman error when the image gives
  no version.
- `blob_compare.py wait` and `compare` (tb-prod, W 4.6) need the `stop-legacy-stalwart` row after
  the last `cut-ecs` row. `wait --apply` polls rule `tb-prod-cutover` until both pending metrics
  read 0 three times after the stop and the failed operations since `REPL_FAILED_SINCE` sum to 0,
  then writes a `blob-replication-wait` row. `compare --apply` without that row runs the same
  wait first (same `--timeout` and `--interval`, the row written only on a pass) and compares
  only after a pass; a timeout stops with exit 1, no row and no copy. `compare` lists both buckets
  in parallel by key range (current versions; a delete marker or an absent key in tb-prod is
  missing), compares size and ETag, and with `--apply` copies each difference from legacy and
  writes a `blob-compare left N` row. It deletes nothing. The blobs `STALWART_WEBADMIN`,
  `STALWART_SPAM_TRAIN_DATA.lz4` and `STALWART_SPAM_CLASSIFIER_MODEL.lz4` must match by checksum,
  or the run stops; Stalwart stores them under the base32 of the blob key in its own alphabet
  (`knkectcxifjfix7xivbecrcnjfha` and so on), which is what the compare looks up and prints beside
  the name. `--workers` defaults to 48 (one `aws` process per page, PR 204: a review measured 16
  minutes for the dry run and 9 for the relist of `--apply` at 48). Listings stay under
  `$EVIDENCE_DIR/blob-compare`, copies in transit under `$SECRET_DIR`.
- The listing does not start the aws CLI per page. Each shard keeps one HTTPS connection to
  `<bucket>.s3.eu-central-1.amazonaws.com` and signs `ListObjectVersions` (SigV4) with credentials
  from `aws configure export-credentials`, fetched once per read-only profile and again when they
  expire within 5 minutes. Copies still use the CLI. After the listing it prints
  `listed <side> pages=<n> versions=<n> in <s>s (<w> workers, one connection per shard)` for each
  side. A page on a kept-alive connection is estimated, not measured, at 0.3 to 0.6 s (the CLI took
  1.7 to 1.8 s), so the about 21,800 pages of both buckets at 16 workers take about 7 to 14
  minutes; `## Done when` step 8 of the Tooling issue measures it. `BLOB_S3_ENDPOINT` is for the
  tests only (`BLOB_TEST=1` and a loopback `http://` address).
- `setenv`, `setenv_secret` and the env file edits of W4 hold `flock` on `$ENV_FILE.lock`;
  `check-prereqs.sh` reads the env file under the same lock, shared.
- `window/w7-since-flip.sh` counts `message-ingest.ham|spam` (inbound) and
  `queue.authenticated-message-queued` (outbound) in the Stalwart logs since the `dns-cutover`
  start for a rollback decision. `window/test-sender-password.sh --apply` reads the app password
  created in the Accounts UI from `TEST_SENDER_APP_PASSWORD` (or `TEST_USER_APP_PASSWORD`) in `$OPERATOR_ENV` (default
  `~/.local/state/talos/cutover-operator-$TARGET_ENV.env`, mode 600), else from stdin, never from an argument.
  It stores the value (appends `TEST_SENDER_APP_PASSWORD`) only after a JMAP session on 443, an
  IMAP LOGIN on 993 and an SMTP AUTH on 587 all succeed, and names the ports that failed. `build-cutover-env.sh` carries
  `TEST_SENDER_APP_PASSWORD` only from the previous cutover env file, never from the operator file,
  so run this script after W5 to set it.
- `cf-record.sh ZONE_ID upsert|delete NAME TYPE [CONTENT]` is dry-run by default; an SPF upsert edits
  the one `v=spf1` TXT at NAME in place, empty content is refused, and it refuses to delete `spf.*`
  or touch `_externaldns.*`.
- `freight_for SHA` (window/lib.sh), used by W5 and W7, selects the Freight whose preview commit has the same
  `overlays/tb-stage-thundermail/stalwart-configs` and `bases/stalwart-configs` trees as SHA
  (preview commits never carry the merge SHA), and stops when TMD main has changed those paths since
  SHA or no Freight matches. `T6_SHA` comes from the env file or, when empty, from the merge commit
  of `T6_PR`, and W5 appends it. T7 and T8 read `T7_PR`/`T7_REVERT_PR` and `T8_PR`/`T8_REVERT_PR`.
