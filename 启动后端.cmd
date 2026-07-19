@echo off
setlocal

set "INVOKED_ROOT=%~dp0"
set "PY_EXE=%INVOKED_ROOT%backend.venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=%INVOKED_ROOT%backend\.venv\Scripts\python.exe"

if not exist "%PY_EXE%" (
  echo ERROR: backend Python was not found.
  echo Expected backend.venv\Scripts\python.exe
  echo Or backend\.venv\Scripts\python.exe
  goto end
)

set "PROJECT_ROOT="
set "ROOT_CAPTURE=%TEMP%\tvf-runtime-root-%RANDOM%-%RANDOM%.txt"
pushd "%INVOKED_ROOT%backend" >nul
"%PY_EXE%" -m app.core.runtime_identity --print-root > "%ROOT_CAPTURE%"
set "IDENTITY_EXIT=%ERRORLEVEL%"
popd >nul
if not "%IDENTITY_EXIT%"=="0" (
  del /q "%ROOT_CAPTURE%" >nul 2>&1
  echo ERROR: physical repository root could not be resolved.
  goto end
)
set /p "PROJECT_ROOT="<"%ROOT_CAPTURE%"
del /q "%ROOT_CAPTURE%" >nul 2>&1
if not defined PROJECT_ROOT (
  echo ERROR: physical repository root was empty.
  goto end
)

cd /d "%PROJECT_ROOT%"
call :check_root
if errorlevel 1 goto end

if /I "%~1"=="--check" (
  echo Physical repository root: %PROJECT_ROOT%
  echo Backend startup script check passed.
  goto end_no_pause
)

echo Starting Trend Video Factory backend...
set "BACKEND_PORT=8002"
echo Backend URL: http://127.0.0.1:%BACKEND_PORT%
echo Close this window to stop the backend.
echo.

"%PY_EXE%" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port %BACKEND_PORT%

if errorlevel 1 (
  echo.
  echo ERROR: Backend failed to start.
)
goto end

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
echo.
pause

:end_no_pause
endlocal
