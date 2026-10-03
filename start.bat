@echo off
REM ===========================================================================
REM OSIA - Launcher (Windows)  -- WOR-11
REM
REM   * activates the venv
REM   * launches the OSIA GUI in wake mode as a detached desktop process
REM
REM The mobile server is owned by its per-user Scheduled Task. Before the GUI
REM is woken, osia.relaunch performs a source-fingerprint + /health readiness
REM check and reuses or replaces that exact server task through the existing
REM service lifecycle. Manage login startup from OSIA Settings > Startup.
REM ===========================================================================
chcp 65001 >nul
setlocal

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERREUR] venv introuvable. Lancez d'abord install.bat.
    endlocal
    exit /b 1
)

REM --- Activate the venv for this shell --------------------------------------
call "%VENV_DIR%\Scripts\activate.bat"

REM --- Background service (mobile server) -----------------------------------
REM It runs as the independent per-user Scheduled Task created by install.bat,
REM starts at login, and survives GUI restarts, so it is not started here. The
REM GUI self-heals it at launch (osia/main.py -> _ensure_mobile_server_windows).

REM --- OSIA GUI - wake mode (detached from this short-lived launcher) --------
echo [start] Verification du serveur canonique puis activation d'OSIA...
pushd "%PROJECT_DIR%"
"%VENV_PY%" -m osia.relaunch --launch-desktop
set "OSIA_START_EXIT=%ERRORLEVEL%"
popd

if not "%OSIA_START_EXIT%"=="0" (
    echo [ERREUR] La capsule OSIA n'a pas pu etre lancee.
    endlocal
    exit /b %OSIA_START_EXIT%
)

echo [start] Runtime canonique sain; OSIA reveille.
endlocal
