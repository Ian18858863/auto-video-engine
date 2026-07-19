@echo off
setlocal

cd /d "%~dp0"
call :check_root
if errorlevel 1 goto end

set "PY_EXE=backend.venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=backend\.venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

"%PY_EXE%" -m app.materials.calibrate_jianying %*
set "EXIT_CODE=%ERRORLEVEL%"
goto end_no_pause

:check_root
if not exist "backend\" goto root_error
if not exist "frontend\" goto root_error
if not exist "TASKS.md" goto root_error
exit /b 0

:root_error
echo Please put this startup file in the project root folder.
echo The correct folder contains backend, frontend, and TASKS.md.
exit /b 1

:end
set "EXIT_CODE=1"
if not "%TVF_NO_PAUSE%"=="1" pause

:end_no_pause
endlocal & exit /b %EXIT_CODE%
