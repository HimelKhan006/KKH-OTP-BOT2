@echo off
setlocal EnableDelayedExpansion
title Push Thirdwave Bot to GitHub
cls
echo ========================================================
echo    PUSH THIRDWAVE BOT TO GITHUB (BOT FILES ONLY)
echo ========================================================
echo.
set "REPO_URL=https://github.com/HimelKhan006/KKH-OTP-BOT2.git"
echo Target Repo: !REPO_URL!
echo.
echo [1/4] Initializing Git repository...
cd /d "%~dp0"
git init >nul 2>&1
git config user.name "HimelKhan006"
git config user.email "himelkhan006@gmail.com"

echo.
echo [2/4] Staging bot.py and workflow (excluding local DB & secrets)...
git rm -rf --cached . >nul 2>&1
git add "bot.py"
git add ".github/workflows/run_bot.yml"
git status -s

echo.
echo [3/4] Creating commit...
git commit -m "Update bot - dedicated bot1 database & 28h Gist persistence" >nul 2>&1
git branch -M main

echo.
echo [4/4] Pushing to GitHub...
git remote remove origin >nul 2>&1
git remote add origin !REPO_URL!
git push -u origin main --force

if !ERRORLEVEL! EQU 0 (
    echo.
    echo ========================================================
    echo  SUCCESS! Bot files pushed to GitHub successfully!
    echo  URL: !REPO_URL!
    echo ========================================================
) else (
    echo.
    echo [WARNING] Push failed. Check your internet connection or credentials.
)
echo.
pause
