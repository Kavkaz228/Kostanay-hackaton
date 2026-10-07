#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
docker info >/dev/null
sha256sum -c images.tar.sha256
docker load --input images.tar
docker run --rm --pull never --network none --user 0 --entrypoint python --mount "type=bind,source=$(pwd),target=/workspace" --workdir /workspace allur-twin-api:2.0.0 ops/init.py
docker compose up -d --pull never --wait web backup
echo 'Allur twin 2.0 is ready. Default address: http://localhost:8088 (PORT in .env).'
echo 'Initial login: admin. Password: .secrets/bootstrap_password. Change it after login.'
