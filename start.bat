@echo off
setlocal
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Failed to install the required Python packages.
    pause
    exit /b 1
)

python "%~dp0main.py" %*
