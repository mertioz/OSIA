@echo off
REM ===========================================================================
REM OSIA - Launcher (Windows)  -- WOR-11
REM
REM   * activates the venv
REM   * launches the OSIA GUI in wake mode in a visible window (blocks here)
REM
REM The mobile server (http://localhost:8765) and the Cloudflare tunnel are NOT
REM started here: each runs as its own per-user Scheduled Task (registered by
REM install.bat) that starts at login and survives GUI restarts — the Windows
REM equivalent of the Linux osia-server / osia-tunnel systemd units. The GUI also
REM self-heals the server at launch (osia/main.py -> _ensure_mobile_server_
REM windows). Manage both from OSIA Settings > Startup.
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

REM --- Background services (mobile server + Cloudflare tunnel) ---------------
REM Both run as independent per-user Scheduled Tasks created by install.bat
REM ("OSIA Mobile Server" / "OSIA Cloudflare Tunnel"); they start at login and
REM survive GUI restarts, so neither is started here. The GUI self-heals the
REM server at launch (osia/main.py -> _ensure_mobile_server_windows). Toggle
REM either one from OSIA Settings > Startup.

REM --- OSIA GUI - wake mode (visible window; blocks until OSIA exits) --------
echo [start] Lancement d'OSIA en mode wake...
"%VENV_PY%" "%PROJECT_DIR%osia\main.py" --wake

endlocal
