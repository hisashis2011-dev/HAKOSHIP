@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === MBS keihi seisan demo ===
REM demo data goes to a fresh folder (old demo DB is ignored)
set DATA_DIR=%~dp0data_demo
python -m pip install -q -r requirements.txt || goto :err
python -m app.seed --demo || goto :err
echo.
echo Open http://127.0.0.1:8000 in your browser. Login: staff@example.com / demo-password-1
echo Close this window or press Ctrl+C to stop.
start "" http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo *** The server stopped. Read the error above. ***
pause
exit /b
:err
echo.
echo *** An error occurred. Copy the message above and send it. ***
pause
