#!/usr/bin/env bash
set -euo pipefail
DRY=0
for a in "$@"; do [ "$a" = --dry-run ] && DRY=1; done
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
web_image() { kstage -n accounts get deploy accounts-web -o jsonpath='{.spec.template.spec.containers[?(@.name=="accounts")].image}'; }
if [ "${1:-}" = --capture ]; then
  out=$EVIDENCE_DIR/django-plan-$TS
  if [ "$DRY" = 1 ]; then
    printf 'dry-run: kubectl -n accounts exec deploy/accounts-web -- ./manage.py showmigrations --plan --no-color > %s.txt\n' "$out"
    printf 'dry-run: deploy/accounts-web container accounts image > %s.image\n' "$out"
    exit 0
  fi
  image=$(web_image)
  [ -n "$image" ] || die "no accounts container image on deploy/accounts-web"
  plan=$(kstage -n accounts exec deploy/accounts-web -c accounts -- ./manage.py showmigrations --plan --no-color | sed -n 's/^\[[ X]\] *\([^ ]*\).*/\1/p' | sort -u)
  [ -n "$plan" ] || die "showmigrations returned no migrations"
  printf '%s\n' "$plan" > "$out.txt"
  printf '%s\n' "$image" > "$out.image"
  printf 'captured migrations=%s image=%s\n%s.txt\n' "$(grep -c . "$out.txt")" "$image" "$out"
  exit 0
fi
need ACCOUNTS_LEGACY_PG_HOST ACCOUNTS_LEGACY_PG_USER ACCOUNTS_LEGACY_PG_NAME
PLAN=${1:-$(ls -1t "$EVIDENCE_DIR"/django-plan-*.txt 2>/dev/null | head -1 || true)}
[ -s "$PLAN" ] || die "no captured plan: run gate-django-migrations.sh --capture while accounts-web is up"
[ -s "${PLAN%.txt}.image" ] || die "${PLAN%.txt}.image missing next to $PLAN"
captured=$(cat "${PLAN%.txt}.image")
query="select app||'.'||name from django_migrations"
if [ "$DRY" = 1 ]; then
  printf 'dry-run: plan %s (%s migrations) captured for %s\n' "$PLAN" "$(grep -c . "$PLAN")" "$captured"
  printf 'dry-run: compare with the deploy/accounts-web image, then pod psql -h %s -U <%s chars> -d %s -Atc "%s"\n' "$ACCOUNTS_LEGACY_PG_HOST" "${#ACCOUNTS_LEGACY_PG_USER}" "$ACCOUNTS_LEGACY_PG_NAME" "$query"
  exit 0
fi
image=$(web_image)
[ "$image" = "$captured" ] || die "GATE FAIL: deploy/accounts-web image changed since the capture ($captured -> $image); recapture while web is up"
applied=$(pod psql -h "$ACCOUNTS_LEGACY_PG_HOST" -U "$ACCOUNTS_LEGACY_PG_USER" -d "$ACCOUNTS_LEGACY_PG_NAME" -Atc "$query" | sort -u)
pending=$(comm -23 "$PLAN" <(printf '%s\n' "$applied") | grep -c . || true)
printf 'image=%s image_migrations=%s applied_on_main=%s pending=%s\n' "$image" "$(grep -c . "$PLAN")" "$(grep -c . <<<"$applied")" "$pending"
[ "$pending" = 0 ] && echo 'GATE PASS' || { comm -23 "$PLAN" <(printf '%s\n' "$applied"); echo 'GATE FAIL: pending migrations on main'; exit 1; }
