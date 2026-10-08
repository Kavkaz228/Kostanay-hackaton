#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
docker compose --profile ai stop
echo 'Stopped. Database, models and installation settings are preserved.'
