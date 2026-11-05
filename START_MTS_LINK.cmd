@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0bootstrap.py"
    if errorlevel 1 pause
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0bootstrap.py"
    if errorlevel 1 pause
    exit /b
)
echo Python 3 was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Then launch START_MTS_LINK.cmd again.
pause
exit /b 1
