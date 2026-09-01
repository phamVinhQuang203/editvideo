@echo off
setlocal
cd /d "%~dp0"

set "GITNEXUS_ENTRY=%~dp0tools\GitNexus-main\gitnexus\dist\cli\index.js"
if not exist "%GITNEXUS_ENTRY%" (
  echo [ERROR] GitNexus has not been built.
  echo Run npm.cmd install and npm.cmd run build in tools\GitNexus-main\gitnexus.
  exit /b 1
)

node "%GITNEXUS_ENTRY%" %*
