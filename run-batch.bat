@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ================================================
echo  Manga Batch Translator
echo ================================================
echo Working dir: %CD%
echo.

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
  echo Install Python 3.10+ from https://python.org.
  echo.
  pause
  exit /b 1
)

if not exist "venv\Scripts\python.exe" (
  echo Creating virtual environment...
  !PYCMD! -m venv venv
  if errorlevel 1 (
    echo Failed to create venv.
    pause
    exit /b 1
  )
  echo Installing dependencies...
  "venv\Scripts\python.exe" -m pip install --upgrade pip
  "venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency install failed.
    pause
    exit /b 1
  )
  echo.
)

echo Launching batch translator...
echo.
"venv\Scripts\python.exe" batch.py
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
