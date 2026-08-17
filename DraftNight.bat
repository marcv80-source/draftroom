@echo off
rem ============================================================================
rem DraftNight.bat -- launch the offline live draft assistant.
rem
rem Starts the draftroom server in --draft mode (installs and verifies the
rem socket guard: no outbound non-localhost connection is possible), waits for
rem its health endpoint, then opens it in the default browser in "app mode"
rem (no tabs/address bar). Everything here is local: no network call is made
rem by this script itself, and the server refuses to start if it cannot prove
rem it is offline-only. Safe to run with wifi physically off.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8484"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "URL=http://127.0.0.1:%PORT%"

if not exist "%VENV_PY%" (
    echo [DraftNight] Could not find the virtualenv Python at "%VENV_PY%".
    echo [DraftNight] Create it first: python -m venv .venv ^&^& .venv\Scripts\pip install -e .
    pause
    exit /b 1
)

echo [DraftNight] Starting the server (offline draft mode) on %URL% ...
start "draftroom server" /min "%VENV_PY%" -m draftroom.server --draft --port %PORT%

echo [DraftNight] Waiting for the health endpoint to come up...
set /a TRIES=0

:waitloop
set /a TRIES+=1
powershell -NoProfile -Command ^
    "try { $r = Invoke-WebRequest -Uri '%URL%/healthz' -UseBasicParsing -TimeoutSec 1; exit ($(if ($r.StatusCode -eq 200) {0} else {1})) } catch { exit 1 }"
if not errorlevel 1 goto :ready

if %TRIES% GEQ 40 (
    echo [DraftNight] Server did not respond after 20 seconds.
    echo [DraftNight] Check the minimized "draftroom server" window for errors.
    goto :openanyway
)
timeout /t 1 /nobreak >nul
goto :waitloop

:ready
echo [DraftNight] Server is up.

:openanyway
echo [DraftNight] Opening %URL% in app mode...

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%CHROME%" (
    start "" "%CHROME%" --app=%URL% --new-window
    goto :done
)
set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%CHROME%" (
    start "" "%CHROME%" --app=%URL% --new-window
    goto :done
)
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE%" (
    start "" "%EDGE%" --app=%URL%
    goto :done
)
set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE%" (
    start "" "%EDGE%" --app=%URL%
    goto :done
)

rem Last resort: whatever the OS default browser is (not "app mode", but still local/offline).
start "" "%URL%"

:done
endlocal
