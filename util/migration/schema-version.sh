#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need STALWART_TARGET_PG_HOST STALWART_TARGET_PG_USER STALWART_TARGET_PG_DB
pod psql -h "$STALWART_TARGET_PG_HOST" -U "$STALWART_TARGET_PG_USER" -d "$STALWART_TARGET_PG_DB" -Atc "select encode(v,'hex') from p where k='\x00'"
