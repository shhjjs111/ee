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

rem 开发模式启动，一个入口同时包住前端和后端：
rem   - 前端走 vite dev server：改 .tsx/.css 即时生效，不用手动 npm run build
rem     （build 一次实测 17 秒，放在每次启动的路径上太重）
rem   - electron 连 VITE_DEV_SERVER_URL；主进程改动由 dev:electron 里的 tsc 重新编译
rem   - 后端由 electron 拉起 python -m ee serve，读仓库里的 ee/*.py；改后端重启本窗口即可
rem 关掉 ee 窗口时 concurrently -k 会把 vite 一起收掉，不留后台进程。
echo [ee] Starting (dev)...
call npm run dev
if errorlevel 1 (
  echo.
  echo [ee] Failed to start. See the messages above.
  pause
)
endlocal
