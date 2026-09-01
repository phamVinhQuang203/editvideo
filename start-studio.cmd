@echo off
REM VIDEO STUDIO — dashboard lam video (KHONG dang bai). Mo http://127.0.0.1:7995
setlocal
cd /d "%~dp0"
chcp 65001 >nul

REM Bo sung tool da cai bang winget/npm khi PATH cua shortcut cu chua cap nhat
if exist "%ProgramFiles%\nodejs" set "PATH=%ProgramFiles%\nodejs;%PATH%"
if exist "%APPDATA%\npm" set "PATH=%APPDATA%\npm;%PATH%"
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin") do set "PATH=%%~fD;%PATH%"

REM Chon python: uu tien venv cua goi (may moi) -> venv autovoice-tool (may cu) -> python he thong
set VPY=
if exist "venv\Scripts\python.exe" set VPY=venv\Scripts\python.exe
if "%VPY%"=="" if exist "E:\CODEX\autovoice-tool\.venv\Scripts\python.exe" set VPY=E:\CODEX\autovoice-tool\.venv\Scripts\python.exe
if "%VPY%"=="" set VPY=python

set PYTHONIOENCODING=utf-8
echo Dung python: %VPY%
echo Mo trinh duyet: http://127.0.0.1:7995
"%VPY%" app\server.py
pause
