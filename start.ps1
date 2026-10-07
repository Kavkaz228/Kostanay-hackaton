$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop and retry.' }
    docker run --rm --mount "type=bind,source=$PSScriptRoot,target=/workspace" --workdir /workspace python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d python ops/init.py
    if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize installation secrets.' }
    docker compose up --build -d --wait web backup
    if ($LASTEXITCODE -ne 0) { throw 'Startup failed. Inspect: docker compose logs --tail 100' }
    $settings = docker compose config --format json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the application address.' }
    $applicationPort = $settings.services.web.ports[0].published
    Write-Host "Allur twin 2.0 is ready: http://localhost:$applicationPort"
} finally {
    Pop-Location
}
