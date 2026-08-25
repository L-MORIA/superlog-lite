@echo off
title Superlog-lite: monitoring ik_llama:8083 (auto-fix)
setlocal

echo ============================================================
echo  SUPERLOG-LITE: monitoring ik_llama:8083
echo  %~dp0monitor.py
echo  - checks /v1/models + trial generation
echo  - incidents stored in SQLite (incidents.db)
echo  - auto-fix: server restart on crash (cooldown 600s)
echo ============================================================

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [error] python not found in PATH. Install Python 3.11+
  pause
  exit /b 1
)

python monitor.py %*

if errorlevel 1 (
  echo [warn] monitor.py exited with code %errorlevel%
)

echo.
echo ============================================================
echo  Done. Incident memory: %~dp0incidents.db
echo  For help: python monitor.py --help
echo ============================================================
pause
