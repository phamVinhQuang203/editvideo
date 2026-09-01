@echo off
setlocal
cd /d "%~dp0"
set "VPY=python"
if exist "venv\Scripts\python.exe" set "VPY=venv\Scripts\python.exe"
if not exist "venv\Scripts\python.exe" if exist "E:\CODEX\autovoice-tool\.venv\Scripts\python.exe" set "VPY=E:\CODEX\autovoice-tool\.venv\Scripts\python.exe"

set "PYTHONIOENCODING=utf-8"
"%VPY%" "app\server.py" >nul 2>&1
