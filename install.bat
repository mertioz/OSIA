@echo off
REM ===========================================================================
REM OSIA - One-click installer (Windows)  -- WOR-11
REM
REM   * verifies Python 3.10+ is installed
REM   * creates a venv in the project directory
REM   * installs all dependencies from requirements.txt
REM   * migrates any existing config\ into %USERPROFILE%\.osia and ensures a
REM     mobile pairing token exists
REM   * registers the mobile server as a per-user Scheduled Task (start at
REM     login, independent of the GUI) and starts it now
REM   * registers the OSIA GUI to launch at login (per-user HKCU Run key)
REM ===========================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "OSIA_HOME=%USERPROFILE%\.osia"
set "OSIA_HAD_STATE=0"
if exist "%OSIA_HOME%" set "OSIA_HAD_STATE=1"
if exist "%VENV_PY%" set "OSIA_HAD_STATE=1"
set "WINDOWS_DIR=%PROJECT_DIR%windows"

REM Run from the project root so the in-tree `osia` package is importable by the
REM inline `python -c` token step below, regardless of the caller's directory.
cd /d "%PROJECT_DIR%"

echo ============================================================
echo   OSIA - Installation
echo ============================================================

REM --- 1. Python 3.10+ check -------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python introuvable. Installez Python 3.10+ depuis https://www.python.org/downloads/ ^(cochez "Add python.exe to PATH"^).
    goto :fail
)
python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)"
if errorlevel 1 (
    echo [ERREUR] Python 3.10 ou superieur est requis.
    python --version
    goto :fail
)
echo [1/7] Python detecte :
python --version

REM --- 2. Virtual environment ------------------------------------------------
if exist "%VENV_PY%" (
    echo [2/7] venv deja present - reutilisation.
) else (
    echo [2/7] Creation du venv...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERREUR] Echec de la creation du venv.
        goto :fail
    )
)

REM --- 3. Dependencies -------------------------------------------------------
echo [3/7] Installation des dependances ^(peut prendre plusieurs minutes^)...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%PROJECT_DIR%requirements.txt"
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    goto :fail
)

REM --- 4. Config dir (%USERPROFILE%\.osia) + mobile token --------------------
echo [4/7] Configuration ^(%OSIA_HOME%^)...
if "%OSIA_HAD_STATE%"=="0" (
    set "OSIA_ONBOARDING_MARKER=%OSIA_HOME%\onboarding.json"
    "%VENV_PY%" -c "import os; from pathlib import Path; from osia.onboarding import create_fresh_install_marker; create_fresh_install_marker(Path(os.environ['OSIA_ONBOARDING_MARKER']))"
    set "OSIA_ONBOARDING_MARKER="
)
if not exist "%OSIA_HOME%" mkdir "%OSIA_HOME%"
REM Migrate any existing repo config\ files without overwriting newer ones.
for %%F in (api_keys.json settings.json mobile_token.txt) do (
    if exist "%PROJECT_DIR%config\%%F" if not exist "%OSIA_HOME%\%%F" copy /Y "%PROJECT_DIR%config\%%F" "%OSIA_HOME%\%%F" >nul
)
REM Ensure a mobile pairing token exists (writes %USERPROFILE%\.osia\mobile_token.txt).
REM PYTHONPATH guarantees `osia` is importable even if cd above failed.
set "PYTHONPATH=%PROJECT_DIR%"
"%VENV_PY%" -c "from osia.config import load_mobile_token, regenerate_mobile_token; load_mobile_token() or regenerate_mobile_token()"

REM --- 5. Private-network-only LAN firewall rule -----------------------------
REM Elevation is requested once during installation. Normal application runtime
REM never creates or broadens firewall rules and therefore never prompts again.
echo [5/7] Configuration du pare-feu LAN ^(reseaux prives uniquement^)...
set "OSIA_FIREWALL_SCRIPT=%WINDOWS_DIR%\configure_lan_firewall.ps1"
set "OSIA_FIREWALL_PYTHON=%VENV_PY%"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%OSIA_FIREWALL_SCRIPT%" -Action Install
if errorlevel 1 (
    echo [ERREUR] La regle pare-feu privee OSIA n'a pas pu etre configuree.
    goto :fail
)

REM --- 6. Background services (Scheduled Tasks) ------------------------------
REM Register the mobile server as a per-user Scheduled Task that starts at login
REM and runs independently of the OSIA GUI. Historical OSIA tunnel tasks are
REM removed by the migration-safe service helper. The server is also started now
REM so a fresh install works without a re-login. Needs no admin.
echo [6/7] Enregistrement du service en arriere-plan ^(serveur mobile^)...
"%VENV_PY%" -c "from osia.utils.systemd_manager import install_windows_services; print('Services:', 'OK' if install_windows_services() else 'partiel')"
set "PYTHONPATH="

REM --- 7. GUI autostart at login (per-user HKCU Run key) ---------------------
REM Launch the OSIA interface automatically at login, after the server task.
REM Per-user (HKCU) so it needs no admin. The value is kept identical to
REM osia.utils.assets._windows_autostart_command() - the single source of truth
REM the Settings toggle "Lancer OSIA au demarrage" reads via autostart_enabled()
REM and writes via set_autostart() - so install.bat seeds it and the GUI toggle
REM can later add/remove it without drift.
echo [7/7] Activation du lancement d'OSIA au demarrage...
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "MAIN_PY=%PROJECT_DIR%osia\main.py"
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "OSIA" /t REG_SZ /d "\"%VENV_PYW%\" \"%MAIN_PY%\" --wake" /f >nul
if errorlevel 1 (
    echo [ATTENTION] Lancement au demarrage non active - vous pourrez l'activer depuis Parametres ^> Demarrage.
)

echo.
echo OSIA installe avec succes. Lancez start.bat pour demarrer.
endlocal
exit /b 0

:fail
echo.
echo Installation interrompue.
endlocal
exit /b 1
