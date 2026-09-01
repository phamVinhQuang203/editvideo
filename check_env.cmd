@echo off
REM Kiem tra moi truong. Them --fix de tu cai goi pip con thieu.
cd /d "%~dp0"
set VPY=python
if exist "venv\Scripts\python.exe" set VPY=venv\Scripts\python.exe
"%VPY%" check_env.py %*
pause
