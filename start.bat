@echo off
REM ===========================================================================
REM OSIA - Launcher (Windows)  -- WOR-11
REM
REM   * activates the venv
REM   * launches the OSIA GUI in wake mode in a visible window (blocks here)
REM
REM The mobile server (http://localhost:8765) is NOT started here: it runs as a
REM per-user Scheduled Task (registered by install.bat) that starts at login and
REM survives GUI restarts. The GUI also self-heals the server at launch
REM (osia/main.py -> _ensure_mobile_server_windows). Manage it from OSIA
REM Settings > Startup.
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

REM --- OSIA GUI - wake mode (visible window; blocks until OSIA exits) --------
echo [start] Lancement d'OSIA en mode wake...
"%VENV_PY%" "%PROJECT_DIR%osia\main.py" --wake

endlocal
