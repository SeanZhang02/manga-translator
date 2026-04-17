@echo off
REM One-click launcher for Manga Live Translator prototype.
REM Serves the folder on http://localhost:8088 so the Anthropic API call
REM doesn't run into file:// CORS weirdness. Requires Python in PATH.

cd /d "%~dp0"

echo.
echo ========================================
echo  Manga Live Translator - local server
echo ========================================
echo.
echo Opening http://localhost:8088 in your browser...
echo Keep this window open while using the app.
echo Press Ctrl+C here to stop the server.
echo.

start "" http://localhost:8088/index.html

python -m http.server 8088

echo.
echo Server stopped.
pause
