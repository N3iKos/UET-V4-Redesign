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

python main.py
if errorlevel 1 (
    echo.
    echo [ERROR] Exited with error.
    pause
)
