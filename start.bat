@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3.13 -c "import sys" >nul 2>nul
    if %errorlevel%==0 set "PY=py -3.13"
)

%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install the required Python packages.
    pause
    exit /b 1
)

%PY% main.py
