param([Parameter(Mandatory=$true)][string]$Backup)
$ErrorActionPreference = 'Stop'
$resolved = (Resolve-Path -LiteralPath $Backup).Path
if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {throw 'Backup file does not exist.'}
$name = 'allur-restore-check-' + [guid]::NewGuid().ToString('N').Substring(0,12)
try {
    docker run -d --name $name --network none -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17.6-alpine | Out-Null
    if ($LASTEXITCODE -ne 0) {throw 'Cannot start isolated restore container.'}
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        docker exec $name pg_isready -U postgres 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {break}
        Start-Sleep -Seconds 1
    }
    docker cp $resolved "${name}:/tmp/restore.dump"
    if ($LASTEXITCODE -ne 0) {throw 'Cannot copy backup.'}
    docker exec $name createdb -U postgres restored
    if ($LASTEXITCODE -ne 0) {throw 'Cannot create isolated database.'}
    docker exec $name pg_restore -U postgres -d restored --no-owner --no-acl --exit-on-error /tmp/restore.dump
    if ($LASTEXITCODE -ne 0) {throw 'Restore failed.'}
    docker exec $name psql -U postgres -d restored -v ON_ERROR_STOP=1 -c "SELECT key, data->>'source' AS source FROM snapshots; SELECT count(*) AS history_count FROM history; SELECT count(*) AS incident_count FROM incidents;"
    if ($LASTEXITCODE -ne 0) {throw 'Restored data validation failed.'}
    Write-Host 'Backup restored and queried successfully in an isolated container. Production database was not changed.'
} finally {
    docker rm -f -v $name 2>$null | Out-Null
}
