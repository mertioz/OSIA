@echo off
REM ===========================================================================
REM OSIA - Start the Cloudflare tunnel as a hidden background process (Windows)
REM Replaces the Linux osia-tunnel.service. -- WOR-11
REM
REM Exposes the local OSIA mobile server (http://127.0.0.1:8765) via a
REM Cloudflare quick tunnel. The public https://*.trycloudflare.com URL is
REM written to windows\tunnel.log.
REM
REM WOR-67: --config NUL makes cloudflared skip ~/.cloudflared/config.yml (whose
REM ingress only routes osia.space and 404s everything else); 127.0.0.1 forces
REM IPv4 to match the server's 0.0.0.0:8765 (IPv4-only) bind.
REM ===========================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
set "CLOUDFLARED=%SCRIPT_DIR%cloudflared.exe"
set "LOGFILE=%SCRIPT_DIR%tunnel.log"

if not exist "%CLOUDFLARED%" (
    echo [tunnel] cloudflared.exe not found. Run install_tunnel.bat first.
    endlocal
    exit /b 1
)

REM Avoid stacking duplicate tunnels - stop any existing instance first.
tasklist /FI "IMAGENAME eq cloudflared.exe" /NH | find /I "cloudflared.exe" >nul
if not errorlevel 1 (
    echo [tunnel] A cloudflared process is already running - leaving it as is.
    endlocal
    exit /b 0
)

echo [tunnel] Launching cloudflared (hidden). Public URL will be in "%LOGFILE%".
REM Paths are passed via env vars ($env:) so an apostrophe in the install path
REM (e.g. C:\Users\O'Brien\...) cannot break the single-quoted PowerShell string.
powershell -NoProfile -Command "Start-Process -FilePath $env:CLOUDFLARED -ArgumentList 'tunnel','--no-autoupdate','--config','NUL','--logfile',$env:LOGFILE,'--url','http://127.0.0.1:8765' -WindowStyle Hidden"
endlocal
exit /b 0
