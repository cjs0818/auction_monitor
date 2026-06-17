@echo off
setlocal enabledelayedexpansion

REM 가상환경 활성화
if not exist .venv\Scripts\python.exe (
    echo 환경이 없습니다. scripts\setup_mac.sh --recreate-venv 를 먼저 실행하십시오.
    pause
    exit /b 1
)

REM 브라우저 자동 열기 (약 2초 후)
timeout /t 2 /nobreak >nul
start "" "http://localhost:8501"

REM Streamlit 실행
.venv\Scripts\python -I -m streamlit run app.py --logger.level=warning

pause
