@echo off
setlocal
chcp 65001 >nul

set "INVOKED_ROOT=%~dp0"
set "PY_EXE=%INVOKED_ROOT%backend.venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=%INVOKED_ROOT%backend\.venv\Scripts\python.exe"

if not exist "%PY_EXE%" (
  echo ERROR: backend Python was not found.
  echo Expected backend.venv\Scripts\python.exe
  echo Or backend\.venv\Scripts\python.exe
  goto error_pause
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
  goto error_pause
)
set /p "PROJECT_ROOT="<"%ROOT_CAPTURE%"
del /q "%ROOT_CAPTURE%" >nul 2>&1
if not defined PROJECT_ROOT (
  echo ERROR: physical repository root was empty.
  goto error_pause
)

cd /d "%PROJECT_ROOT%"
call :check_root
if errorlevel 1 goto error_pause

if not exist "frontend\package.json" (
  echo ERROR: frontend\package.json was not found.
  goto error_pause
)

if not exist "frontend\node_modules\.bin\vite.cmd" (
  echo ERROR: frontend dependencies were not found.
  echo Please run npm install in the frontend folder first.
  goto error_pause
)

if /I "%~1"=="--check" (
  echo Physical repository root: %PROJECT_ROOT%
  echo One-click startup script check passed.
  goto done
)

echo Starting Auto Video Engine...
set "BACKEND_PORT=8002"
set "FRONTEND_PORT=5176"
set "VITE_BACKEND_URL=http://127.0.0.1:%BACKEND_PORT%"
set "VITE_FRONTEND_PORT=%FRONTEND_PORT%"
echo Backend:  http://127.0.0.1:%BACKEND_PORT%
echo Frontend: http://127.0.0.1:%FRONTEND_PORT%
echo Browser will open automatically.
echo.

start "Auto Video Engine Backend" /D "%PROJECT_ROOT%" cmd /k ""%PY_EXE%" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port %BACKEND_PORT%"
timeout /t 2 /nobreak >nul

start "Auto Video Engine Frontend" /D "%PROJECT_ROOT%\frontend" cmd /k "set VITE_BACKEND_URL=%VITE_BACKEND_URL%&& set VITE_FRONTEND_PORT=%VITE_FRONTEND_PORT%&& call node_modules\.bin\vite.cmd --host 127.0.0.1 --port %FRONTEND_PORT%"
timeout /t 5 /nobreak >nul

start "" "http://127.0.0.1:%FRONTEND_PORT%"
goto done

:check_root
if not exist "backend\" goto root_error
if not exist "frontend\" goto root_error
if not exist "TASKS.md" goto root_error
exit /b 0

:root_error
echo Please put this startup file in the project root folder.
echo The correct folder contains backend, frontend, and TASKS.md.
exit /b 1

:error_pause
echo.
pause

:done
endlocal
