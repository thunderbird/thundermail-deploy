#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
DUMP=${1:-}
[ -n "$DUMP" ] || die "usage: restore-target-db.sh /work/<file>.dump [--apply] | --assert-only"
need STALWART_TARGET_PG_HOST STALWART_TARGET_PG_USER STALWART_TARGET_PG_DB STALWART_LEGACY_PG_HOST
expected=$(yq -r '.stalwartPgHost' "$REPO_DIR/overlays/tb-stage-thundermail/stalwart-configs/config/values.yaml" | sed 's/-pooler//')
[ "$STALWART_TARGET_PG_HOST" = "$expected" ] || die "target host does not match the tb-stage overlay host (direct form)"
[ "$STALWART_TARGET_PG_HOST" != "$STALWART_LEGACY_PG_HOST" ] || die "target host equals the legacy host"
[ "$STALWART_TARGET_PG_DB" = neondb ] || die "target database is not neondb"
db=$(pod psql -h "$STALWART_TARGET_PG_HOST" -U "$STALWART_TARGET_PG_USER" -d "$STALWART_TARGET_PG_DB" -Atc 'select current_database()')
[ "$db" = neondb ] || die "current_database() returned $db"
[ "$DUMP" = --assert-only ] && { printf 'assertions passed: host=%s db=%s (no dump checked)\n' "$STALWART_TARGET_PG_HOST" "$db"; exit 0; }
pod sh -c "test -s '$DUMP'" || die "$DUMP missing or empty in the runner"
printf 'assertions passed: host=%s db=%s dump_bytes=%s\n' "$STALWART_TARGET_PG_HOST" "$db" "$(pod sh -c "ls -l '$DUMP' | awk '{print \$5}'")"
printf 'would run via pod:\n  psql -h %s -U %s -d %s -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"\n  pg_restore -j4 --no-owner --no-privileges -h %s -U %s -d %s %s\n' "$STALWART_TARGET_PG_HOST" "$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_DB" "$STALWART_TARGET_PG_HOST" "$STALWART_TARGET_PG_USER" "$STALWART_TARGET_PG_DB" "$DUMP"
[ "$APPLY" = 1 ] || { echo 'mode=dry-run'; exit 0; }
pod psql -h "$STALWART_TARGET_PG_HOST" -U "$STALWART_TARGET_PG_USER" -d "$STALWART_TARGET_PG_DB" -v ON_ERROR_STOP=1 -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
pod sh -c "pg_restore -j4 --no-owner --no-privileges -h '$STALWART_TARGET_PG_HOST' -U '$STALWART_TARGET_PG_USER' -d '$STALWART_TARGET_PG_DB' '$DUMP' 2>/work/restore.err; rc=\$?; grep -vc 'schema \"public\" already exists' /work/restore.err; exit \$rc"
echo 'mode=apply restore finished'
