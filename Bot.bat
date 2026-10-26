@echo off
rem Graphony: with AUTOPILOT on (the default) this runs the autopilot's jobs at their times (generate, post,
rem insights, health); with AUTOPILOT=false in .env, the Telegram approval bot. Keep this window open.
setlocal
cd /d "%~dp0"
title Graphony bot
if not exist ".venv\Scripts\python.exe" (
  echo Graphony is not installed in this folder yet ^(.venv is missing^). Follow "Setup" in README.md, then try again.
  pause
  exit /b 1
)
echo Graphony is running. Keep this window open; close it to stop it.
".venv\Scripts\python.exe" run.py bot
pause
