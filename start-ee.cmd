@echo off
setlocal
title ee
cd /d "%~dp0desktop"

rem Node/npm must be on PATH; give a clear message instead of a flashing window.
where npx >nul 2>nul
if errorlevel 1 (
  echo [ee] Node.js not found on PATH. Install Node 20+ and try again.
  pause
  exit /b 1
)

if not exist "node_modules" (
  echo [ee] Desktop dependencies missing. Running npm install once...
  call npm install
)

echo [ee] Starting...
call npx electron .
if errorlevel 1 (
  echo.
  echo [ee] Failed to start. See the messages above.
  pause
)
endlocal
