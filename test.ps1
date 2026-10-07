param([switch]$Load)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$compose = $null
try {
    $settings = docker compose config --format json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $settings.name) { throw 'Cannot resolve the installation project.' }
    $testProject = $settings.name + '-check'
    $compose = @('compose','-p',$testProject,'-f','compose.yaml','-f','compose.test.yaml')
    docker run --rm -e TEST_INSTALLATION=true --mount "type=bind,source=$PSScriptRoot,target=/workspace" --workdir /workspace python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d python ops/init.py
    if ($LASTEXITCODE -ne 0) {throw 'Test secrets initialization failed.'}
    docker @compose build tests e2e api web
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    docker @compose run --rm --no-deps tests
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
    docker @compose up -d --wait web
    if ($LASTEXITCODE -ne 0) { throw 'Isolated test application failed to start.' }
    docker @compose run --rm --no-deps e2e
    if ($LASTEXITCODE -ne 0) { throw 'Browser tests failed. See e2e/results.' }
    if ($Load) {
        docker @compose run --rm --no-deps -e LOAD_TEST_ALLOWED=isolated-only -e PYTHONPATH=/app tests python /ops/load_test.py
        if ($LASTEXITCODE -ne 0) {throw 'Load acceptance criteria failed.'}
    }
    Write-Host 'All requested tests passed. Customer data and secrets were not used.'
} finally {
    if ($compose) { docker @compose stop }
    Pop-Location
}
