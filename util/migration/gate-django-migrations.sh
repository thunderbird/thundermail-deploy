#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need ACCOUNTS_LEGACY_PG_HOST ACCOUNTS_LEGACY_PG_USER ACCOUNTS_LEGACY_PG_NAME
plan=$(kstage -n accounts exec deploy/accounts-web -- ./manage.py showmigrations --plan --no-color 2>/dev/null | sed -n 's/^\[[ X]\] *\([^ ]*\).*/\1/p' | sort -u)
[ -n "$plan" ] || die "showmigrations returned no migrations"
applied=$(pod psql -h "$ACCOUNTS_LEGACY_PG_HOST" -U "$ACCOUNTS_LEGACY_PG_USER" -d "$ACCOUNTS_LEGACY_PG_NAME" -Atc "select app||'.'||name from django_migrations" | sort -u)
pending=$(comm -23 <(printf '%s\n' "$plan") <(printf '%s\n' "$applied") | grep -c . || true)
printf 'image_migrations=%s applied_on_main=%s pending=%s\n' "$(grep -c . <<<"$plan")" "$(grep -c . <<<"$applied")" "$pending"
[ "$pending" = 0 ] && echo 'GATE PASS' || { comm -23 <(printf '%s\n' "$plan") <(printf '%s\n' "$applied"); echo 'GATE FAIL: pending migrations on main'; exit 1; }
