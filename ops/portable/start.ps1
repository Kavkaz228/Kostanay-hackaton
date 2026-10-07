$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker with Linux containers and retry.' }
    $archive = Join-Path $PSScriptRoot 'images.tar'
    $expected = (Get-Content -LiteralPath "$archive.sha256" -Raw).Trim().Split(' ')[0]
    if ($expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
        throw 'Image archive checksum mismatch. Extract a fresh copy of the release.'
    }
    docker load --input $archive
    if ($LASTEXITCODE -ne 0) { throw 'Cannot load the release images.' }
    docker run --rm --pull never --network none --user 0 --entrypoint python --mount "type=bind,source=$PSScriptRoot,target=/workspace" --workdir /workspace allur-twin-api:2.0.0 ops/init.py
    if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize installation secrets.' }
    docker compose up -d --pull never --wait web backup
    if ($LASTEXITCODE -ne 0) { throw 'Startup failed. Inspect: docker compose logs --tail 100' }
    $settings = docker compose config --format json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the application address.' }
    Write-Host "Allur twin 2.0 is ready: http://localhost:$($settings.services.web.ports[0].published)"
    Write-Host 'Initial login: admin. Password: .secrets/bootstrap_password. Change it after login.'
} finally { Pop-Location }
