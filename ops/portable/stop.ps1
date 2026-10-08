$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    docker compose --profile ai stop
    if ($LASTEXITCODE -ne 0) { throw 'Docker could not stop the application.' }
    Write-Host 'Stopped. Database, models and installation settings are preserved.'
} finally { Pop-Location }
