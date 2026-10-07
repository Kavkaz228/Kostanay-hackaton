@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Allur twin 2.0
echo Запуск Allur twin 2.0...
docker info >nul 2>&1
if errorlevel 1 (
  echo.
  echo Docker Desktop не запущен или не установлен.
  echo 1. Установите Docker Desktop: https://www.docker.com/products/docker-desktop/
  echo 2. Запустите его и дождитесь статуса Engine running.
  echo 3. Снова дважды щёлкните этот файл.
  start "" "https://www.docker.com/products/docker-desktop/"
  pause
  exit /b 1
)
echo Готовлю установку и собираю приложение. Первый запуск занимает 3-7 минут...
docker run --rm --mount "type=bind,source=%CD%,target=/workspace" --workdir /workspace python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d python ops/init.py
if errorlevel 1 goto fail
docker compose up --build -d --wait web backup
if errorlevel 1 goto fail
set "APPPORT=8088"
for /f "tokens=2 delims=:" %%a in ('docker compose port web 8080 2^>nul') do set "APPPORT=%%a"
set /p ADMINPASS=<.secrets\bootstrap_password
echo.
echo Allur twin 2.0 готов: http://localhost:%APPPORT%
echo Логин: admin
echo Пароль: %ADMINPASS%
echo После первого входа приложение попросит задать свой пароль (от 15 символов).
start "" "http://localhost:%APPPORT%"
echo.
echo Окно можно закрыть: приложение продолжит работать. Остановить: stop-windows.bat
pause
exit /b 0
:fail
echo.
echo Запуск не удался. Журнал: docker compose logs --tail 100
pause
exit /b 1
