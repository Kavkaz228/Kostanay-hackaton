param([string]$Project = '')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not $Project) {
        $settings = docker compose config --format json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the installation project.' }
        $Project = $settings.name
        if (-not $Project) { throw 'Installation project is not configured.' }
    }
    New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'backups') -Force | Out-Null
    $name = 'allur-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.dump'
    docker compose -p $Project exec -T db pg_dump -U allur -d allur -Fc --no-owner -f "/tmp/$name"
    if ($LASTEXITCODE -ne 0) { throw 'Backup failed. Existing data were not changed.' }
    docker compose -p $Project exec -T db pg_restore --list "/tmp/$name" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Backup validation failed.' }
    docker compose -p $Project cp "db:/tmp/$name" "backups/$name"
    if ($LASTEXITCODE -ne 0) { throw 'Backup copy failed.' }
    $path = Join-Path $PSScriptRoot "backups/$name"
    (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash | Set-Content -LiteralPath "$path.sha256"
    Write-Host "Backup saved: $path"
} finally { Pop-Location }
