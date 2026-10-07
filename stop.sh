#!/bin/sh
# Остановка Allur twin 2.0 на macOS и Linux (аналог stop.ps1). База и состояние модели сохраняются.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
docker compose --profile ai stop
echo 'Остановлено. База данных и состояние симуляции сохранены.'
