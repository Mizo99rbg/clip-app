@echo off
cd /d "%~dp0"
echo Closing any old copy of the app...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
call venv\Scripts\activate.bat
start "" http://127.0.0.1:8000
python -m uvicorn app:app --port 8000
