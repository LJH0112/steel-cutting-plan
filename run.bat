@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if errorlevel 1 goto :error

rem Keep an existing older .venv intact. Windows always uses Python 3.13 here.
set "STEELPLAN_VENV=.venv-py313"
py -3.13 -c "import sys; print('Python:', sys.version); sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)"
if errorlevel 1 goto :missing_python

if not exist "%STEELPLAN_VENV%\Scripts\python.exe" (
  py -3.13 -m venv "%STEELPLAN_VENV%"
  if errorlevel 1 goto :error
)
"%STEELPLAN_VENV%\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)"
if errorlevel 1 goto :wrong_venv

"%STEELPLAN_VENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
if not defined STEELPLAN_HOST set "STEELPLAN_HOST=127.0.0.1"
if not defined STEELPLAN_PORT set "STEELPLAN_PORT=8000"
echo.
echo STEELPLAN: http://%STEELPLAN_HOST%:%STEELPLAN_PORT%
echo 서버를 종료하려면 Ctrl+C를 누르세요.
"%STEELPLAN_VENV%\Scripts\python.exe" -m uvicorn app:app --host "%STEELPLAN_HOST%" --port "%STEELPLAN_PORT%"
if errorlevel 1 goto :error
echo 서버가 종료되었습니다.
pause
exit /b 0

:missing_python
echo.
echo Python 3.13 및 Windows Python Launcher가 필요합니다.
echo Python 3.13을 설치하고 이 파일을 다시 실행하세요. 다른 Python 버전으로 실행하지 않습니다.
pause
exit /b 1

:wrong_venv
echo.
echo .venv-py313의 Python 버전이 3.13이 아닙니다.
echo 해당 폴더를 백업 또는 이름 변경한 후 다시 실행하세요. 기존 환경을 자동 삭제하지 않습니다.
pause
exit /b 1

:error
echo.
echo STEELPLAN 실행에 실패했습니다. 위 오류 메시지를 확인하세요.
echo 이 창은 키를 누를 때까지 닫히지 않습니다.
pause
exit /b 1
