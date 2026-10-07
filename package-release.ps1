$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    docker compose build api web
    if ($LASTEXITCODE -ne 0) { throw 'Image build failed.' }
    docker image inspect postgres:17.6-alpine *> $null
    if ($LASTEXITCODE -ne 0) {
        docker pull postgres:17.6-alpine
        if ($LASTEXITCODE -ne 0) { throw 'Cannot obtain PostgreSQL image.' }
    }
    $containerArgs = @('run','--rm','--network','none','--user','0','--entrypoint','python','--mount',"type=bind,source=$PSScriptRoot,target=/workspace",'--workdir','/workspace','allur-twin-api:2.0.0')
    docker @containerArgs ops/package_release.py prepare
    if ($LASTEXITCODE -ne 0) { throw 'Release preparation failed.' }
    docker save --output 'output/Allur-twin-2.0/images.tar' allur-twin-api:2.0.0 allur-twin-web:2.0.0 postgres:17.6-alpine
    if ($LASTEXITCODE -ne 0) { throw 'Image export failed.' }
    docker @containerArgs ops/package.py
    if ($LASTEXITCODE -ne 0) { throw 'Source packaging failed.' }
    docker @containerArgs ops/package_release.py finish
    if ($LASTEXITCODE -ne 0) { throw 'Release verification failed.' }
    Write-Host 'Allur twin 2.0: output/Allur-twin-2.0-docker.zip'
} finally { Pop-Location }
