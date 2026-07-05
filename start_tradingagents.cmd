@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_tradingagents.ps1" %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo TradingAgents exited with code %EXITCODE%.
  pause
)

exit /b %EXITCODE%
