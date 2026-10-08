@echo off
rem Graphony dashboard: double-click to open it in your browser. Close this window to stop it.
setlocal
cd /d "%~dp0"
title Graphony dashboard
set "URL=http://127.0.0.1:5055/"
if not exist ".venv\Scripts\python.exe" (
  echo Graphony is not installed in this folder yet ^(.venv is missing^). Follow "Setup" in README.md, then try again.
  pause
  exit /b 1
)
rem Already running? Then only open the browser.
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%ping').Content -eq 'graphony-dashboard') { exit 0 } } catch {}; exit 1"
if %errorlevel%==0 (
  start "" "%URL%"
  exit /b 0
)
rem Start the dashboard in this window; open the browser once it answers (waits up to 15 s).
start "" /b powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; $t=[Diagnostics.Stopwatch]::StartNew(); while ($t.Elapsed.TotalSeconds -lt 15) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 '%URL%ping' | Out-Null; break } catch { Start-Sleep -Milliseconds 300 } }; Start-Process '%URL%'"
".venv\Scripts\python.exe" run.py dashboard
pause
