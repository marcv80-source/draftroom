@echo off
rem ============================================================================
rem Prep.bat -- run the online prep pipeline: fetch fresh sources, then resolve
rem the player-identity crosswalk offline against the completeness gate.
rem
rem This is the ONLINE half of the two-phase architecture (repo CLAUDE.md).
rem Run it whenever you want fresher ADP/projections before draft night; never
rem run it on draft night itself.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [Prep] Could not find the virtualenv Python at "%VENV_PY%".
    echo [Prep] Create it first: python -m venv .venv ^&^& .venv\Scripts\pip install -e .
    pause
    exit /b 1
)

echo ============================================================
echo  draftroom prep pipeline
echo ============================================================
echo.
echo [1/2] fetch_all  (network -- Sleeper, FFC, FantasyPros if configured)
echo ------------------------------------------------------------
"%VENV_PY%" -m draftroom.prep.fetch_all
set "FETCH_RC=%errorlevel%"

echo.
echo [2/2] resolve_cli  (offline -- crosswalk + top-200 completeness gate)
echo ------------------------------------------------------------
"%VENV_PY%" -m draftroom.prep.resolve_cli
set "RESOLVE_RC=%errorlevel%"

echo.
echo ============================================================
echo  SUMMARY
echo ============================================================
echo   fetch_all  : ran (see per-source status/notes above; a SKIPPED or
echo                ERROR row does not fail this script -- check the table)
if "%RESOLVE_RC%"=="0" (
    echo   resolve_cli: PASS -- every top-200 FFC ADP player resolved
) else (
    echo   resolve_cli: FAIL -- unresolved players in the top 200
    echo                see data\unresolved_report.csv, triage into data\overrides.csv
)
echo ============================================================

if not "%RESOLVE_RC%"=="0" (
    exit /b 1
)
exit /b 0
