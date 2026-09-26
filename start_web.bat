@echo off
rem ===================================================================
rem  One-click launcher for the Zentao bug auto-fix console.
rem  Usage:
rem     start_web.bat            start web + open browser
rem     start_web.bat sync       additionally pull assigned bugs from Zentao first
rem ===================================================================
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [error] python not found in PATH, install Python 3.9+ first.
  pause
  exit /b 1
)

python -c "import flask, requests, dotenv" >nul 2>nul
if errorlevel 1 (
  echo [info] installing dependencies ...
  python -m pip install -r requirements.txt
)

set ARGS=
if /i "%~1"=="sync" set ARGS=--sync
python run_web.py %ARGS%

pause
