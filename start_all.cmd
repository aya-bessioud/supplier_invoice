@echo off
REM Starts the 2 local services of the prototype, each in its own window.
REM To stop: close the two windows (or Ctrl+C in each).  Ollama runs on its own (system tray).
cd /d "%~dp0"
start "OCR service (port 8000)" powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\start_ocr.ps1"
start "n8n (port 5678)" powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\start_n8n.ps1"
echo.
echo  Wait ~30 seconds, then open:
echo    n8n editor : http://localhost:5678
echo    Invoice form: http://localhost:5678/form/invoice-upload
echo.
timeout /t 30 >nul
start "" "http://localhost:5678/form/invoice-upload"
