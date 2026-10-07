#!/bin/sh
set -eu
umask 077
export PGPASSWORD="$(cat /run/secrets/database_password)"
while true; do
    file="/backups/allur-$(date -u +%Y%m%dT%H%M%SZ).dump"
    pg_dump -h db -U allur -d allur -Fc --no-owner -f "$file.tmp"
    pg_restore --list "$file.tmp" > /dev/null
    mv "$file.tmp" "$file"
    sha256sum "$file" > "$file.sha256"
    touch /backups/last-success
    echo "Database backup completed: $(basename "$file")"
    find /backups -maxdepth 1 -type f -name 'allur-*.dump*' -mtime +14 -delete
    sleep 21600
done
