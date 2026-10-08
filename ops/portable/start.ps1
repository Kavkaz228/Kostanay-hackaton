$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker with Linux containers and retry.' }
    $archive = Join-Path $PSScriptRoot 'images.tar'
    $expected = (Get-Content -LiteralPath "$archive.sha256" -Raw).Trim().Split(' ')[0]
    Write-Host 'Verifying offline Docker images (this can take a few minutes)...'
    if ($expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
        throw 'Image archive checksum mismatch. Extract a fresh copy of the release.'
    }
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') -Raw | ConvertFrom-Json
    $needsLoad = $false
    foreach ($image in $manifest.images) {
        # Windows PowerShell 5 treats redirected native stderr as an ErrorRecord.
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $actual = docker image inspect $image.name --format '{{.Id}}' 2>$null
        $inspectExit = $LASTEXITCODE
        $ErrorActionPreference = $savedPreference
        if ($inspectExit -ne 0 -or $actual -ne $image.id) { $needsLoad = $true }
    }
    if ($needsLoad) {
        docker load --input $archive
        if ($LASTEXITCODE -ne 0) { throw 'Cannot load the release images.' }
    } else { Write-Host 'Bundled image versions are already installed.' }
    $helper = @('run','--rm','--pull','never','--network','none','--user','0','--entrypoint','python','--mount',"type=bind,source=$PSScriptRoot,target=/workspace",'--workdir','/workspace','allur-twin-api:2.0.0','ops/portable_runtime.py')
    docker @helper verify
    if ($LASTEXITCODE -ne 0) { throw 'Release file verification failed.' }
    docker @helper init
    if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize installation secrets.' }
    $settings = docker compose --profile ai config --format json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Invalid Compose configuration.' }
    $modelVolume = $settings.volumes.ollama_models.name
    docker volume create --label "com.docker.compose.project=$($settings.name)" --label 'com.docker.compose.volume=ollama_models' $modelVolume | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the local model volume.' }
    docker run --rm --pull never --network none --user 0 --entrypoint python --mount "type=bind,source=$PSScriptRoot,target=/workspace,readonly" --volume "${modelVolume}:/modelroot" --workdir /workspace allur-twin-api:2.0.0 ops/portable_runtime.py import-model
    if ($LASTEXITCODE -ne 0) { throw 'Offline model import failed. Existing files were not overwritten.' }
    docker compose --profile ai up -d --pull never --wait web backup ollama
    if ($LASTEXITCODE -ne 0) { throw 'Startup failed. Inspect: docker compose --profile ai logs --tail 100' }
    Write-Host "Allur Twin is ready: http://localhost:$($settings.services.web.ports[0].published)"
    Write-Host 'Initial login: admin. Password: .secrets/bootstrap_password. Change it after login.'
} finally { Pop-Location }
