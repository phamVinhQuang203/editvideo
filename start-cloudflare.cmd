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

REM Bat Video Studio ngam neu cong 7995 chua san sang.
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:7995/login -TimeoutSec 2; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Dang bat Video Studio...
  start "" wscript.exe "%~dp0start-studio-hidden.vbs"
  powershell -NoProfile -Command "$ok=$false; 1..30 | ForEach-Object { if(-not $ok){ try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:7995/login -TimeoutSec 2 | Out-Null; $ok=$true } catch { Start-Sleep -Seconds 1 } } }; if(-not $ok){ exit 1 }"
  if errorlevel 1 (
    echo [!] Video Studio khong khoi dong duoc. Thu chay start-studio.cmd de xem loi.
    pause
    exit /b 1
  )
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
