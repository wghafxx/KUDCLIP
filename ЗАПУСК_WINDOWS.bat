@echo off
setlocal
title KUDCLIP - start
echo ==========================================
echo   KUDCLIP - YouTube auto-clips
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://www.python.org/downloads/ and check "Add to PATH"
    pause
    exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install from https://nodejs.org/
    pause
    exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ffmpeg not found in PATH. Download https://www.gyan.dev/ffmpeg/builds/ and add bin folder to PATH.
    pause
    exit /b 1
)
where ffprobe >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ffprobe not found. Add the ffmpeg bin folder to PATH.
    pause
    exit /b 1
)

cd /d "%~dp0"

if not exist "backend\.env" (
    echo CORS_ORIGINS=*> "backend\.env"
    echo Created backend\.env
)
if not exist "frontend\.env" (
    echo REACT_APP_BACKEND_URL=http://localhost:8001> "frontend\.env"
    echo Created frontend\.env
)

echo.
echo [1/3] Installing backend dependencies...
cd backend
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the Python environment.
        pause
        exit /b 1
    )
)
set "PATH=%CD%\.venv\Scripts;%PATH%"
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Backend dependency installation failed.
    pause
    exit /b 1
)
cd ..

echo [2/3] Installing frontend dependencies (first run takes a few minutes)...
cd frontend
where yarn >nul 2>nul
if errorlevel 1 (
    call npm install --silent
) else (
    call yarn install --silent
)
if errorlevel 1 (
    echo [ERROR] Frontend dependency installation failed.
    pause
    exit /b 1
)
cd ..

echo [3/3] Starting...
set REACT_APP_BACKEND_URL=http://localhost:8001
start "KUDCLIP Backend" cmd /k "cd /d ""%~dp0backend"" && python run_server.py --host 0.0.0.0 --port 8001"
timeout /t 3 /nobreak >nul
where yarn >nul 2>nul
if errorlevel 1 (
    start "KUDCLIP Frontend" cmd /k "cd /d ""%~dp0frontend"" && npm start"
) else (
    start "KUDCLIP Frontend" cmd /k "cd /d ""%~dp0frontend"" && yarn start"
)

echo.
echo Done! http://localhost:3000 will open in ~30 seconds.
echo Groq keys and Telegram token are entered inside the app and saved forever.
timeout /t 25 /nobreak >nul
start http://localhost:3000
exit /b 0
