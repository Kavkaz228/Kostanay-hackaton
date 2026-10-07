@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose --profile ai stop
echo Остановлено. База данных и состояние модели сохранены.
pause
