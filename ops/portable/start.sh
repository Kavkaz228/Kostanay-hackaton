#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
docker info >/dev/null
echo 'Verifying offline Docker images (this can take a few minutes)...'
sha256sum -c images.tar.sha256
needs_load=false
while read -r image expected; do
    actual=$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null || true)
    if [ "$actual" != "$expected" ]; then needs_load=true; fi
done < images.expected.txt
if [ "$needs_load" = true ]; then
    docker load --input images.tar
else
    echo 'Bundled image versions are already installed.'
fi
docker run --rm --pull never --network none --user 0 --entrypoint python --mount "type=bind,source=$(pwd),target=/workspace" --workdir /workspace allur-twin-api:2.0.0 ops/portable_runtime.py verify
docker run --rm --pull never --network none --user 0 --entrypoint python --mount "type=bind,source=$(pwd),target=/workspace" --workdir /workspace allur-twin-api:2.0.0 ops/portable_runtime.py init
# Parse Compose JSON with bundled Python; no Python installation is required on the host.
compose_details=$(docker compose --profile ai config --format json | docker run --rm -i --pull never --network none --entrypoint python allur-twin-api:2.0.0 -c 'import json,sys; c=json.load(sys.stdin); print(c["volumes"]["ollama_models"]["name"] + " " + c["name"])')
model_volume=${compose_details%% *}
project_name=${compose_details#* }
test -n "$model_volume"
test -n "$project_name"
docker volume create --label "com.docker.compose.project=$project_name" --label 'com.docker.compose.volume=ollama_models' "$model_volume" >/dev/null
docker run --rm --pull never --network none --user 0 --entrypoint python --mount "type=bind,source=$(pwd),target=/workspace,readonly" --volume "$model_volume:/modelroot" --workdir /workspace allur-twin-api:2.0.0 ops/portable_runtime.py import-model
docker compose --profile ai up -d --pull never --wait web backup ollama
echo 'Allur Twin is ready. Default address: http://localhost:8088 (PORT in .env).'
echo 'Initial login: admin. Password: .secrets/bootstrap_password. Change it after login.'
