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
  (headers only) and fails unless `From` is on `auth-stage.tb.pro`. Appends the `p3-smtp` timing row.
