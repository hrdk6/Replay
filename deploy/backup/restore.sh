#!/bin/sh
# Restore a pg_dump (custom format) into a database, preserving ownership by the app role.
#
#   PGHOST=... PGPASSWORD=<superuser pw> ./restore.sh /backups/replay-2026....dump replay_restored
#
# Restores into a NEW database first; verify it (deploy/backup/verify_restore.sql), then point
# DATABASE_URL at it or rename databases during a maintenance window. Never restore over the
# live database in place.
set -eu
DUMP="${1:?usage: restore.sh <dump-file> <target-db>}"
TARGET="${2:?usage: restore.sh <dump-file> <target-db>}"
APP_ROLE="${APP_ROLE:-replay}"

psql -v ON_ERROR_STOP=1 -U postgres -d postgres -c "CREATE DATABASE \"$TARGET\" OWNER \"$APP_ROLE\";"
# Restore as the app role so tables (and their RLS policies) stay owned by it.
pg_restore -U postgres -d "$TARGET" --no-owner --role="$APP_ROLE" --exit-on-error "$DUMP"
psql -v ON_ERROR_STOP=1 -U postgres -d "$TARGET" -f "$(dirname "$0")/verify_restore.sql"
echo "restored into $TARGET"
