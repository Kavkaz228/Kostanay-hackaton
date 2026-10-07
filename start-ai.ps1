param([string]$Model = 'qwen3:4b')
$ErrorActionPreference = 'Stop'
if ($Model -match 'cloud|https?://' -or $Model -notmatch '^[a-zA-Z0-9_./:-]+$') {throw 'Use a local model name only.'}
Push-Location $PSScriptRoot
try {
    docker compose --profile ai up -d --wait ollama
    if ($LASTEXITCODE -ne 0) {throw 'Local inference service failed to start.'}
    $configuration = docker compose --profile ai config --format json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {throw 'Cannot read local compose configuration.'}
    $modelVolume = $configuration.volumes.ollama_models.name
    $modelImage = $configuration.services.ollama.image
    # Only this disposable downloader has internet access; it receives no plant data.
    $download = 'ollama serve >/tmp/ollama.log 2>&1 & server=$!; trap "kill $server" EXIT; n=0; until ollama list >/dev/null 2>&1; do n=$((n+1)); [ "$n" -lt 30 ] || exit 1; sleep 1; done; ollama pull "$1"'
    docker run --rm --network bridge --volume "${modelVolume}:/root/.ollama" --env OLLAMA_NO_CLOUD=1 --entrypoint /bin/sh $modelImage -c $download download $Model
    if ($LASTEXITCODE -ne 0) {throw 'Model download failed; factory data have not been sent anywhere.'}
    Write-Host "Local model ready: $Model. For a non-default model set AI_MODEL in .env and recreate api."
} finally {Pop-Location}
