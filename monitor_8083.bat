@echo off
chcp 65001 >nul
title Superlog-lite: мониторинг ik_llama:8083 (auto-fix)
setlocal

echo ============================================================
echo  SUPERLOG-LITE: мониторинг ik_llama:8083
echo  %~dp0monitor.py
echo  - проверка /v1/models + генерация
echo  - инциденты в SQLite (incidents.db)
echo  - auto-fix: перезапуск сервера при падении (cooldown 600s)
echo ============================================================

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [error] python не найден в PATH. Установите Python 3.11+
  pause
  exit /b 1
)

python monitor.py %*

if errorlevel 1 (
  echo [warn] monitor.py завершился с кодом %errorlevel%
)

echo.
echo ============================================================
echo  Готово. Память инцидентов: %~dp0incidents.db
echo  Для справки: python monitor.py --help
echo ============================================================
pause
