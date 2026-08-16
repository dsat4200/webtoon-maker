@echo off
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install the required Python packages.
    pause
    exit /b 1
)

python main.py
