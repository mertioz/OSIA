@echo off
REM Remove OSIA startup integration while preserving all user configuration,
REM pairing identities, certificates, conversations, and secrets.
setlocal
set "PROJECT_DIR=%~dp0"
set "VENV_PY=%PROJECT_DIR%venv\Scripts\python.exe"
set "OSIA_FIREWALL_SCRIPT=%PROJECT_DIR%windows\configure_lan_firewall.ps1"

if exist "%VENV_PY%" (
    set "PYTHONPATH=%PROJECT_DIR%"
    "%VENV_PY%" -c "from osia.utils.systemd_manager import disable_service; disable_service('osia-server.service'); disable_service('osia-tunnel.service')"
)
schtasks /delete /TN "OSIA Wake Recovery" /F >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "OSIA" /f >nul 2>&1

powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%OSIA_FIREWALL_SCRIPT%" -Action Remove
if errorlevel 1 (
    echo [ATTENTION] La regle pare-feu OSIA n'a pas pu etre supprimee.
    exit /b 1
)

echo Integration OSIA supprimee. Les donnees utilisateur et l'association mobile sont conservees.
endlocal
exit /b 0
