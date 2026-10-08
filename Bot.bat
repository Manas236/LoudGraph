@echo off
rem Graphony bot: Telegram approvals, scheduled videos and posting. Keep this window open.
setlocal
cd /d "%~dp0"
title Graphony bot
if not exist ".venv\Scripts\python.exe" (
  echo Graphony is not installed in this folder yet ^(.venv is missing^). Follow "Setup" in README.md, then try again.
  pause
  exit /b 1
)
echo Graphony bot is running. Keep this window open; close it to stop the bot.
".venv\Scripts\python.exe" run.py bot
pause
