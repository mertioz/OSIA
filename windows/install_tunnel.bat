@echo off
REM ===========================================================================
REM OSIA - Cloudflare tunnel installer (Windows)  -- WOR-11
REM
REM Downloads cloudflared.exe (Windows amd64) next to this script and then
REM starts it as a hidden background process exposing the local OSIA mobile
REM server (http://localhost:8765) over a Cloudflare quick tunnel. This is the
REM Windows replacement for the Linux osia-tunnel.service systemd unit.
REM ===========================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
set "CLOUDFLARED=%SCRIPT_DIR%cloudflared.exe"
set "CF_URL=https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

if exist "%CLOUDFLARED%" (
    echo [tunnel] cloudflared.exe already present - skipping download.
) else (
    echo [tunnel] Downloading cloudflared.exe for Windows...
    REM URL/path via env vars ($env:) so apostrophes in the path can't break the
    REM single-quoted PowerShell string.
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:CF_URL -OutFile $env:CLOUDFLARED } catch { Write-Host $_.Exception.Message; exit 1 }"
    if errorlevel 1 (
        echo [tunnel] ERROR: download failed. Check your internet connection.
        endlocal
        exit /b 1
    )
    echo [tunnel] Downloaded to "%CLOUDFLARED%".
)

echo [tunnel] Starting the Cloudflare tunnel in the background...
call "%SCRIPT_DIR%start_tunnel.bat"
endlocal
exit /b 0
