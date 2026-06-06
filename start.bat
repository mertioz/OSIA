@echo off
REM ===========================================================================
REM OSIA - Launcher (Windows)  -- WOR-11
REM
REM   * activates the venv
REM   * starts the mobile server (run_server.py) in the background
REM   * starts the Cloudflare tunnel (start_tunnel.bat) in the background
REM   * launches the OSIA GUI in wake mode in a visible window (blocks here)
REM
REM The mobile server is started because the Cloudflare tunnel exposes it
REM (http://localhost:8765); without it the tunnel/mobile app would have
REM nothing to reach. Run windows\stop_tunnel.bat to stop the tunnel.
REM ===========================================================================
chcp 65001 >nul
setlocal

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"

if not exist "%VENV_PY%" (
    echo [ERREUR] venv introuvable. Lancez d'abord install.bat.
    endlocal
    exit /b 1
)

REM --- Activate the venv for this shell --------------------------------------
call "%VENV_DIR%\Scripts\activate.bat"

REM --- Mobile server (background, detached, no window) -----------------------
echo [start] Demarrage du serveur mobile en arriere-plan...
start "OSIA Server" "%VENV_PYW%" "%PROJECT_DIR%osia\server\run_server.py"

REM --- Cloudflare tunnel (background) ----------------------------------------
echo [start] Demarrage du tunnel Cloudflare en arriere-plan...
call "%PROJECT_DIR%windows\start_tunnel.bat"

REM --- OSIA GUI - wake mode (visible window; blocks until OSIA exits) --------
echo [start] Lancement d'OSIA en mode wake...
"%VENV_PY%" "%PROJECT_DIR%osia\main.py" --wake

endlocal
