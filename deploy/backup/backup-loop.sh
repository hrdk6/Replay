#!/bin/sh
# Nightly logical backup (pg_dump custom format) with retention. Used by the self-host stack.
# Managed Postgres (Render, etc.) has its own automated backups / point-in-time recovery;
# this complements them with portable dumps you control.
set -eu
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
HOST="${PGHOST:-postgres}"
while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  file="/backups/replay-$stamp.dump"
  if pg_dump -h "$HOST" -U postgres -d replay -Fc -f "$file.partial"; then
    mv "$file.partial" "$file"
    echo "backup ok: $file ($(du -h "$file" | cut -f1))"
  else
    echo "backup FAILED at $stamp" >&2
    rm -f "$file.partial"
  fi
  find /backups -name 'replay-*.dump' -mtime "+$KEEP_DAYS" -delete
  sleep 86400
done
