@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "backend" (
  echo Please put this file in the project root folder.
  echo The folder should contain backend, frontend, and TASKS.md.
  pause
  exit /b 1
)

if not exist "backend\.venv\Scripts\python.exe" (
  echo Backend Python venv not found: backend\.venv\Scripts\python.exe
  echo Please install backend dependencies first.
  pause
  exit /b 1
)

backend\.venv\Scripts\python.exe -m app.hotspots.browser_cli --force
echo.
pause
