$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
$compose = @('compose','-p','allur-twin-check','-f','compose.yaml','-f','compose.test.yaml')
$query = "SELECT count(*)::text || '|' || md5(string_agg(md5(data::text), '' ORDER BY robot_id,timestamp)) FROM robot_observations;"
try {
    $before = docker @compose exec -T db psql -U allur -d allur -At -v ON_ERROR_STOP=1 -c $query
    if ($LASTEXITCODE -ne 0) {throw 'Cannot read initial integrity fingerprint.'}
    $restartsBefore = docker inspect allur-twin-check-api-1 --format '{{.RestartCount}}'
    docker @compose restart db
    if ($LASTEXITCODE -ne 0) {throw 'Cannot restart test database.'}
    # Allow the ownership heartbeat to detect its disconnected PostgreSQL session.
    Start-Sleep -Seconds 5
    docker @compose up -d --no-build --wait --wait-timeout 120 web
    if ($LASTEXITCODE -ne 0) {throw 'Application did not recover.'}
    $after = docker @compose exec -T db psql -U allur -d allur -At -v ON_ERROR_STOP=1 -c $query
    if ($LASTEXITCODE -ne 0 -or $before -ne $after) {throw 'Data fingerprint differs after restart.'}
    $restartsAfter = docker inspect allur-twin-check-api-1 --format '{{.RestartCount}}'
    if ([int]$restartsAfter -le [int]$restartsBefore) {throw 'Ownership-loss restart was not observed.'}
    [PSCustomObject]@{fingerprint=$after; restartsBefore=$restartsBefore; restartsAfter=$restartsAfter; result='passed'} | ConvertTo-Json | Set-Content '.test-installation/results/failure-check.json'
    Write-Host "Database connection loss recovered automatically; data fingerprint unchanged: $after"
} finally {Pop-Location}
