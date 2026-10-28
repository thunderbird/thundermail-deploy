# Cutover tooling

Scripts for the tb-stage cutover (platform-infrastructure milestone 1536). Every command
in the in-window issues sources the env file first, then `util/migration/timing.sh`:

```bash
set -a; . ~/.local/state/talos/cutover-tb-stage.env; set +a; . util/migration/timing.sh
```

## Conventions

- `check-prereqs.sh [--for KEY]` prints HAVE/NEED per tool and credential and exits with the
  NEED count. It never prints a value.
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
(not `app=stalwart`, so no Service selects it). The `postgres` container writes
`/work/.pgpass` at start from the mounted Secrets, so no password is ever an exec
argument. Entries are keyed so that each of the four Neon targets resolves:

| line | matches | password |
|---|---|---|
| target host (direct and pooler form, read from the `stalwart-config` ConfigMap) | Stalwart target | `stalwart-postgresql-admin-password` |
| `*:5432:keycloak:*` | legacy Keycloak | `rehearsal-legacy-pg` key `keycloak` |
| `*:5432:neondb:neondb_owner` | legacy Accounts | `rehearsal-legacy-pg` key `accounts` |
| `*:5432:*:*` | legacy Stalwart | `rehearsal-legacy-pg` key `stalwart` |

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
  the dump and exits 1 on any difference (`migrate_v016.py dump` skips failed reads and still
  exits 0); W4 runs it before `convert`. It and `--check` send the legacy admin password only to
  a loopback `OLD_STALWART_URL` owned by this user's session-manager-plugin.
- `restore-target-db.sh` refuses to run unless the target host equals the tb-stage overlay
  host (direct form), differs from the legacy host, and `current_database()` is `neondb`.
- `migrate_v016.py` is the upstream release copy (stalwartlabs/stalwart v0.16.26,
  `resources/scripts/migrate_v016.py`, sha256 `ebc7c2cc8b3d9378476523ee5fd76c39b1bc0e788a890ff17b507b03c0757458`).
  Install its dependencies with `pip install -r util/migration/requirements.txt`.
- `thaw.sh` (run after the freeze PR merges with a merge commit) reverts the merged
  freeze commit on a branch from `main`, opens the draft thaw PR on platform-infrastructure
  and appends `FREEZE_SHA` and `THAW_PR` to the env file. Needs `FREEZE_PR` and `PI_DIR`.
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

`window/w2-cut-legacy.sh`, `w3-blob-copy.sh`, `w4-migrate.sh`, `w5-promote.sh` and
`w6-verify.sh` run one in-window step each. Each sources `$CUTOVER_ENV` (default
`~/.local/state/talos/cutover-tb-stage.env`) itself, so values appended by an earlier step
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
- After the W2 revoke the security group has no ingress rules and a restore re-adds them with new
  ids, so rebuild the env file (`build-cutover-env.sh`) before any W2 rerun.
- W5 selects the Freight whose preview commit has the same
  `overlays/tb-stage-thundermail/stalwart-configs` and `bases/stalwart-configs` trees as `T6_SHA`
  (preview commits never carry the merge SHA). `T6_SHA` comes from the env file or, when empty,
  from the merge commit of `T6_PR`, and W5 appends it.
