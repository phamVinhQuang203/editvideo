@echo off
REM ============================================================
REM   VIDEO STUDIO - SETUP (may moi, 1-click)
REM   Tu cai Python + ffmpeg + Node + codex qua winget/npm,
REM   roi tao venv + cai goi + config + kiem tra.
REM   (Logic that nam trong setup.ps1)
REM ============================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
