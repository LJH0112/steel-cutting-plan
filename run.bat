@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
if not defined STEELPLAN_HOST set STEELPLAN_HOST=127.0.0.1
if not defined STEELPLAN_PORT set STEELPLAN_PORT=8000
.venv\Scripts\python.exe -m uvicorn app:app --host %STEELPLAN_HOST% --port %STEELPLAN_PORT%
