@echo off
REM ===========================================================================
REM OSIA - One-click installer (Windows)  -- WOR-11
REM
REM   * verifies Python 3.10+ is installed
REM   * creates a venv in the project directory
REM   * installs all dependencies from requirements.txt
REM   * downloads cloudflared.exe to windows\
REM   * migrates any existing config\ into %USERPROFILE%\.osia and ensures a
REM     mobile pairing token exists
REM   * registers the mobile server + Cloudflare tunnel as per-user Scheduled
REM     Tasks (start at login, independent of the GUI) and starts them now
REM ===========================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "OSIA_HOME=%USERPROFILE%\.osia"
set "WINDOWS_DIR=%PROJECT_DIR%windows"
set "CLOUDFLARED=%WINDOWS_DIR%\cloudflared.exe"
set "CF_URL=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

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
echo [1/6] Python detecte :
python --version

REM --- 2. Virtual environment ------------------------------------------------
if exist "%VENV_PY%" (
    echo [2/6] venv deja present - reutilisation.
) else (
    echo [2/6] Creation du venv...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERREUR] Echec de la creation du venv.
        goto :fail
    )
)

REM --- 3. Dependencies -------------------------------------------------------
echo [3/6] Installation des dependances ^(peut prendre plusieurs minutes^)...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%PROJECT_DIR%requirements.txt"
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    goto :fail
)

REM --- 4. cloudflared.exe ----------------------------------------------------
if not exist "%WINDOWS_DIR%" mkdir "%WINDOWS_DIR%"
if exist "%CLOUDFLARED%" (
    echo [4/6] cloudflared.exe deja present.
) else (
    echo [4/6] Telechargement de cloudflared.exe...
    REM Paths/URL are passed via env vars so apostrophes in them cannot break
    REM the single-quoted PowerShell string.
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:CF_URL -OutFile $env:CLOUDFLARED } catch { Write-Host $_.Exception.Message; exit 1 }"
    if errorlevel 1 (
        echo [ATTENTION] Telechargement de cloudflared echoue - vous pourrez relancer windows\install_tunnel.bat plus tard.
    )
)

REM --- 5. Config dir (%USERPROFILE%\.osia) + mobile token --------------------
echo [5/6] Configuration ^(%OSIA_HOME%^)...
if not exist "%OSIA_HOME%" mkdir "%OSIA_HOME%"
REM Migrate any existing repo config\ files without overwriting newer ones.
for %%F in (api_keys.json settings.json mobile_token.txt) do (
    if exist "%PROJECT_DIR%config\%%F" if not exist "%OSIA_HOME%\%%F" copy /Y "%PROJECT_DIR%config\%%F" "%OSIA_HOME%\%%F" >nul
)
REM Ensure a mobile pairing token exists (writes %USERPROFILE%\.osia\mobile_token.txt).
REM PYTHONPATH guarantees `osia` is importable even if cd above failed.
set "PYTHONPATH=%PROJECT_DIR%"
"%VENV_PY%" -c "from osia.config import load_mobile_token, regenerate_mobile_token; print('Mobile token:', load_mobile_token() or regenerate_mobile_token())"

REM --- 6. Background services (Scheduled Tasks) ------------------------------
REM Register the mobile server + Cloudflare tunnel as per-user Scheduled Tasks
REM that start at login and run independently of the OSIA GUI (the Windows
REM equivalent of the Linux osia-server / osia-tunnel systemd units). They are
REM also started now so a fresh install works without a re-login. Needs no admin.
echo [6/6] Enregistrement des services en arriere-plan ^(serveur mobile + tunnel^)...
"%VENV_PY%" -c "from osia.utils.systemd_manager import install_windows_services; print('Services:', 'OK' if install_windows_services() else 'partiel')"
set "PYTHONPATH="

echo.
echo OSIA installe avec succes. Lancez start.bat pour demarrer.
endlocal
exit /b 0

:fail
echo.
echo Installation interrompue.
endlocal
exit /b 1
