@echo off
title Universal Excel Translator v4

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    pause
    exit /b 1
)

python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing requests...
    pip install requests
)

python -c "import openpyxl" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing openpyxl...
    pip install openpyxl
)

python -c "import textual" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing textual...
    pip install textual
)

python main.py
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Application exited with error code %EXIT_CODE%.
    if exist "uet_crash.log" (
        echo Details have been saved to 'uet_crash.log'.
    )
    echo.
    pause
)
