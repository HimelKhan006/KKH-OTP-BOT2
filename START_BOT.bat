@echo off
title THIRDWAVE OTP WAVE Bot
chcp 65001 >nul
color 0A
cd /d "%~dp0"
echo ===================================================
echo     THIRDWAVE OTP WAVE TELEGRAM FORWARDER BOT
echo ===================================================
echo.
echo Killing any old bot instances...
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo Starting bot...
echo (Press Ctrl+C to stop)
echo.
:LOOP
python bot.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [WARNING] Bot crashed (exit code %ERRORLEVEL%). Restarting in 5s...
    timeout /t 5 /nobreak >nul
    goto LOOP
)
pause
