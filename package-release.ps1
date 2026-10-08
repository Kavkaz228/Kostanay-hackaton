param(
    [ValidateSet('all', 'prepare', 'finish')][string]$Phase = 'all',
    [switch]$SkipBuild,
    [string]$ModelVolume = 'allur-twin_ollama_models'
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $bundle = 'output/Allur-Twin-2026-10-08'
    $images = @('allur-twin-api:2.0.0', 'allur-twin-web:2.0.0', 'postgres:17.6-alpine', 'allur-twin-ollama:2026-10-08', 'allur-twin-caddy:2.10.2')
    $containerArgs = @('run','--rm','--pull','never','--network','none','--user','0','--entrypoint','python','--mount',"type=bind,source=$PSScriptRoot,target=/workspace",'--workdir','/workspace','allur-twin-api:2.0.0')
    if ($Phase -ne 'finish') {
        if (-not $SkipBuild) {
            docker compose build api web
            if ($LASTEXITCODE -ne 0) { throw 'Image build failed.' }
        }
        $ollama = 'ollama/ollama@sha256:292ee7945dfc3d5840a181f3ab86fedb1e66703e02c8af98b50f4da56b7e278c'
        docker image tag $ollama allur-twin-ollama:2026-10-08
        if ($LASTEXITCODE -ne 0) { throw 'Install the pinned Ollama image with start-ai.ps1 first.' }
        $caddy = 'caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        docker image inspect $caddy *> $null
        $inspectExit = $LASTEXITCODE
        $ErrorActionPreference = $savedPreference
        if ($inspectExit -ne 0) {
            docker pull --platform linux/amd64 $caddy
            if ($LASTEXITCODE -ne 0) { throw 'Cannot obtain the pinned Caddy image.' }
        }
        docker image tag $caddy allur-twin-caddy:2.10.2
        if ($LASTEXITCODE -ne 0) { throw 'Cannot tag Caddy for offline loading.' }
        docker @containerArgs ops/package_release.py prepare
        if ($LASTEXITCODE -ne 0) { throw 'Release preparation failed.' }
        $metadata = foreach ($image in $images) {
            $inspected = docker image inspect $image | ConvertFrom-Json
            if ($LASTEXITCODE -ne 0) { throw "Required local image is missing: $image" }
            if ($inspected[0].Architecture -ne 'amd64' -or $inspected[0].Os -ne 'linux') { throw "Wrong platform: $image" }
            @{name=$image; id=$inspected[0].Id; architecture='amd64'; os='linux'; repo_digests=@($inspected[0].RepoDigests)}
        }
        ConvertTo-Json -InputObject @($metadata) -Depth 5 | Set-Content -LiteralPath "$bundle/images.metadata.json" -Encoding utf8
        docker save --output "$bundle/images.tar" @images
        if ($LASTEXITCODE -ne 0) { throw 'Image export failed.' }
        docker run --rm --pull never --network none --user 0 --entrypoint python --mount "type=bind,source=$PSScriptRoot,target=/workspace" --volume "${ModelVolume}:/modelroot:ro" --workdir /workspace allur-twin-api:2.0.0 ops/package_release.py export-model
        if ($LASTEXITCODE -ne 0) { throw 'Model export failed.' }
    }
    docker @containerArgs ops/package.py
    if ($LASTEXITCODE -ne 0) { throw 'Source packaging failed.' }
    $finalPhase = if ($Phase -eq 'prepare') { 'index' } else { 'finish' }
    docker @containerArgs ops/package_release.py $finalPhase
    if ($LASTEXITCODE -ne 0) { throw 'Release verification failed.' }
    Write-Host "Release $finalPhase completed: $bundle"
} finally { Pop-Location }
