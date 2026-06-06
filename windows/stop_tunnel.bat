@echo off
REM ===========================================================================
REM OSIA - Stop the Cloudflare tunnel background process (Windows) -- WOR-11
REM Replaces `systemctl stop osia-tunnel.service`.
REM ===========================================================================
setlocal
tasklist /FI "IMAGENAME eq cloudflared.exe" /NH | find /I "cloudflared.exe" >nul
if errorlevel 1 (
    echo [tunnel] No cloudflared process is running.
    endlocal
    exit /b 0
)

echo [tunnel] Stopping cloudflared...
taskkill /IM cloudflared.exe /F >nul 2>&1
if errorlevel 1 (
    echo [tunnel] ERROR: could not stop cloudflared.
    endlocal
    exit /b 1
)
echo [tunnel] Tunnel stopped.
endlocal
exit /b 0
