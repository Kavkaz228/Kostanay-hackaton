#!/bin/sh
# Запуск Allur twin 2.0 на macOS и Linux (аналог start.ps1). Нужен Docker Desktop или Docker Engine.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# Docker не должен вызывать git при сборке (на macOS без инструментов разработчика это открывает окно установки).
export BUILDX_GIT_INFO=0

if ! docker info >/dev/null 2>&1; then
  if [ "$(uname)" = Darwin ] && [ -d /Applications/Docker.app ]; then
    echo 'Запускаю Docker Desktop…'
    open -a Docker
    tries=0
    until docker info >/dev/null 2>&1; do
      tries=$((tries + 1))
      if [ "$tries" -gt 90 ]; then
        echo 'Docker Desktop не запустился за 3 минуты. Откройте его вручную и повторите.' >&2
        exit 1
      fi
      sleep 2
    done
  else
    echo 'Docker не найден или не запущен.' >&2
    echo 'Установите Docker Desktop: https://www.docker.com/products/docker-desktop/ — запустите его и повторите.' >&2
    exit 1
  fi
fi

echo 'Готовлю установку и собираю приложение. Первый запуск занимает 3–7 минут…'
docker run --rm --mount "type=bind,source=$(pwd),target=/workspace" --workdir /workspace \
  python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d python ops/init.py
docker compose up --build -d --wait web backup || {
  echo 'Запуск не удался. Посмотрите журнал: docker compose logs --tail 100' >&2
  exit 1
}
port=$(docker compose port web 8080 2>/dev/null | sed 's/.*://')
url="http://localhost:${port:-8088}"
echo
echo "Allur twin 2.0 готов: $url"
echo "Логин: admin"
echo "Пароль: $(cat .secrets/bootstrap_password)"
echo 'После первого входа приложение попросит задать свой пароль (от 15 символов).'
if command -v open >/dev/null 2>&1; then open "$url"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true; fi
