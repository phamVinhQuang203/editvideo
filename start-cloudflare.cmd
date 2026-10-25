@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo [!] Chua co cloudflared.
  where winget >nul 2>nul
  if errorlevel 1 (
    echo Tai cloudflared tai: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
    pause
    exit /b 1
  )
  echo Dang cai cloudflared qua winget...
  winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
  echo.
  echo Cai xong. Hay dong cua so nay va bam lai start-cloudflare.cmd.
  pause
  exit /b 0
)

REM Khong tu bat Video Studio. Server phai duoc chay foreground trong terminal rieng.
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:7995/login -TimeoutSec 2; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [!] Video Studio chua chay.
  echo Hay mo MOT TERMINAL RIENG, chay start-studio.cmd, roi chay lai file nay.
  pause
  exit /b 1
)

echo.
if exist "%~dp0cloudflare-token.txt" (
  set /p CF_TOKEN=<"%~dp0cloudflare-token.txt"
  if not defined CF_TOKEN (
    echo [!] cloudflare-token.txt dang trong.
    pause
    exit /b 1
  )
  echo Dang mo Cloudflare Tunnel co dinh...
  echo Domain duoc cau hinh trong Cloudflare Zero Trust se tro vao Video Studio.
  cloudflared tunnel --no-autoupdate run --token "%CF_TOKEN%"
) else (
  echo Dang mo Quick Tunnel tam thoi...
  echo Dia chi https://....trycloudflare.com se hien ben duoi.
  echo Giu cua so nay mo de website tiep tuc hoat dong.
  cloudflared tunnel --no-autoupdate --url http://127.0.0.1:7995
)
pause
