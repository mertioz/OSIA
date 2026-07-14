@echo off
REM ===========================================================================
REM OSIA - One-click installer (Windows)  -- WOR-11
REM
REM   * verifies Python 3.10+ is installed
REM   * creates a venv in the project directory
REM   * installs all dependencies from requirements.txt
REM   * downloads cloudflared.exe and ffmpeg.exe to windows\
REM   * migrates any existing config\ into %USERPROFILE%\.osia and ensures a
REM     mobile pairing token exists
REM   * registers the mobile server + Cloudflare tunnel as per-user Scheduled
REM     Tasks (start at login, independent of the GUI) and starts them now
REM   * registers the OSIA GUI to launch at login (per-user HKCU Run key)
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
set "FFMPEG_DST=%WINDOWS_DIR%\ffmpeg.exe"
set "FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"

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
echo [1/8] Python detecte :
python --version

REM --- 2. Virtual environment ------------------------------------------------
if exist "%VENV_PY%" (
    echo [2/8] venv deja present - reutilisation.
) else (
    echo [2/8] Creation du venv...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERREUR] Echec de la creation du venv.
        goto :fail
    )
)

REM --- 3. Dependencies -------------------------------------------------------
echo [3/8] Installation des dependances ^(peut prendre plusieurs minutes^)...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%PROJECT_DIR%requirements.txt"
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    goto :fail
)

REM --- 4. Windows helper binaries (cloudflared + ffmpeg) --------------------
if not exist "%WINDOWS_DIR%" mkdir "%WINDOWS_DIR%"
if exist "%CLOUDFLARED%" (
    echo [4/8] cloudflared.exe deja present.
) else (
    echo [4/8] Telechargement de cloudflared.exe...
    REM Paths/URL are passed via env vars so apostrophes in them cannot break
    REM the single-quoted PowerShell string.
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:CF_URL -OutFile $env:CLOUDFLARED } catch { Write-Host $_.Exception.Message; exit 1 }"
    if errorlevel 1 (
        echo [ATTENTION] Telechargement de cloudflared echoue - vous pourrez relancer windows\install_tunnel.bat plus tard.
    )
)
REM ffmpeg.exe -- needed by the mobile server for WAV->MP3 TTS (server.py
REM _wav_to_mp3 falls back to windows\ffmpeg.exe when none is on PATH). Static
REM win64 build with libmp3lame. Idempotent and non-fatal: the server tries
REM shutil.which("ffmpeg") first, so a failed download only disables bundled
REM TTS, not the install. The zip is extracted to a temp dir and the inner
REM bin\ffmpeg.exe is located by name (folder name varies by build). Paths/URL
REM are passed via env vars so apostrophes in them cannot break the string.
if exist "%FFMPEG_DST%" (
    echo [4/8] ffmpeg.exe deja present.
) else (
    echo [4/8] Telechargement de ffmpeg.exe...
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $zip = Join-Path $env:TEMP 'osia_ffmpeg.zip'; $tmp = Join-Path $env:TEMP 'osia_ffmpeg'; Invoke-WebRequest -Uri $env:FFMPEG_URL -OutFile $zip; if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }; Expand-Archive -Path $zip -DestinationPath $tmp -Force; $exe = Get-ChildItem -Path $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1; if (-not $exe) { throw 'ffmpeg.exe introuvable dans archive' }; Copy-Item -Path $exe.FullName -Destination $env:FFMPEG_DST -Force; Remove-Item -Force $zip; Remove-Item -Recurse -Force $tmp } catch { Write-Host $_.Exception.Message; exit 1 }"
    if errorlevel 1 (
        echo [ATTENTION] Telechargement de ffmpeg echoue - le TTS du serveur mobile sera indisponible jusqu'a l'installation de ffmpeg ^(PATH ou windows\ffmpeg.exe^).
    )
)

REM --- 5. Config dir (%USERPROFILE%\.osia) + mobile token --------------------
echo [5/8] Configuration ^(%OSIA_HOME%^)...
if not exist "%OSIA_HOME%" mkdir "%OSIA_HOME%"
REM Migrate any existing repo config\ files without overwriting newer ones.
for %%F in (api_keys.json settings.json mobile_token.txt) do (
    if exist "%PROJECT_DIR%config\%%F" if not exist "%OSIA_HOME%\%%F" copy /Y "%PROJECT_DIR%config\%%F" "%OSIA_HOME%\%%F" >nul
)
REM Ensure a mobile pairing token exists (writes %USERPROFILE%\.osia\mobile_token.txt).
REM PYTHONPATH guarantees `osia` is importable even if cd above failed.
set "PYTHONPATH=%PROJECT_DIR%"
"%VENV_PY%" -c "from osia.config import load_mobile_token, regenerate_mobile_token; load_mobile_token() or regenerate_mobile_token()"

REM --- 6. Private-network-only LAN firewall rule -----------------------------
REM Elevation is requested once during installation. Normal application runtime
REM never creates or broadens firewall rules and therefore never prompts again.
echo [6/8] Configuration du pare-feu LAN ^(reseaux prives uniquement^)...
set "OSIA_FIREWALL_SCRIPT=%WINDOWS_DIR%\configure_lan_firewall.ps1"
set "OSIA_FIREWALL_PYTHON=%VENV_PY%"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%OSIA_FIREWALL_SCRIPT%" -Action Install
if errorlevel 1 (
    echo [ERREUR] La regle pare-feu privee OSIA n'a pas pu etre configuree.
    goto :fail
)

REM --- 7. Background services (Scheduled Tasks) ------------------------------
REM Register the mobile server + Cloudflare tunnel as per-user Scheduled Tasks
REM that start at login and run independently of the OSIA GUI (the Windows
REM equivalent of the Linux osia-server / osia-tunnel systemd units). They are
REM also started now so a fresh install works without a re-login. Needs no admin.
echo [7/8] Enregistrement des services en arriere-plan ^(serveur mobile + tunnel^)...
"%VENV_PY%" -c "from osia.utils.systemd_manager import install_windows_services; print('Services:', 'OK' if install_windows_services() else 'partiel')"
set "PYTHONPATH="

REM --- 8. GUI autostart at login (per-user HKCU Run key) ---------------------
REM Launch the OSIA interface automatically at login, after the server/tunnel
REM tasks. Per-user (HKCU) so it needs no admin. The value is kept identical to
REM osia.utils.assets._windows_autostart_command() - the single source of truth
REM the Settings toggle "Lancer OSIA au demarrage" reads via autostart_enabled()
REM and writes via set_autostart() - so install.bat seeds it and the GUI toggle
REM can later add/remove it without drift.
echo [8/8] Activation du lancement d'OSIA au demarrage...
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
