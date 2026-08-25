@echo off
title Superlog-lite: monitoring LLM ports 8083->8080 (failover, auto-fix)
setlocal
if not defined SUPERLOG_PORTS set "SUPERLOG_PORTS=8083,8080"

echo ============================================================
echo  SUPERLOG-LITE: monitoring local LLM servers
echo  Ports (failover order): %SUPERLOG_PORTS%
echo  %~dp0monitor.py
echo  - first reachable port is monitored, rest are fallbacks
echo  - per-port incident logs: incidents.db / incidents_8080.db
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
