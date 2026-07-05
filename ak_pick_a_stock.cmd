@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ak_pick_a_stock.ps1" %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo A-share candidate picker exited with code %EXITCODE%.
  pause
)

exit /b %EXITCODE%
