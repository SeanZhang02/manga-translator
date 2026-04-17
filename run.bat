@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ================================================
echo  Manga Live Translator - desktop overlay
echo ================================================
echo Working dir: %CD%
echo.

REM Prefer the py launcher - bypasses the Windows Store python.exe alias
set "PYCMD="
py -3 --version >nul 2>&1
if not errorlevel 1 (
  set "PYCMD=py -3"
) else (
  python --version >nul 2>&1
  if not errorlevel 1 (
    set "PYCMD=python"
  )
)

if "!PYCMD!"=="" (
  echo ERROR: Python not found.
  echo Install Python 3.10+ from https://python.org and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo Python:
!PYCMD! --version
echo.

REM Create venv on first run
if not exist "venv\Scripts\python.exe" (
  echo Creating virtual environment...
  !PYCMD! -m venv venv
  if errorlevel 1 (
    echo.
    echo Failed to create venv. Try running this .bat as administrator,
    echo or make sure your Python install has the "venv" module.
    echo.
    pause
    exit /b 1
  )
  echo Installing dependencies - first time takes 1-2 minutes...
  echo.
  "venv\Scripts\python.exe" -m pip install --upgrade pip
  "venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Dependency install failed. Check your internet and retry.
    echo.
    pause
    exit /b 1
  )
  echo.
  echo Setup complete.
  echo.
)

echo Launching app...
echo.
"venv\Scripts\python.exe" app.py
set "EC=!ERRORLEVEL!"

echo.
if not "!EC!"=="0" (
  echo App exited with error code !EC!
) else (
  echo App closed normally.
)
echo.
pause
endlocal
