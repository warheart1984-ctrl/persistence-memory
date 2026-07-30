@echo off
REM Start Jarvis Memory Board server (launched by scheduled task at logon)
set "PYTHON=C:\Users\My PC\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
set "ROOT=G:\Mandala Rendering Software\jarvis-memoryboard"
set "LOG=%ROOT%\data\jarvis.log"
set "PORT=8001"

echo [%DATE% %TIME%] Starting Jarvis Memory Board on port %PORT%... >> "%LOG%"
"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --log-level warning >> "%LOG%" 2>&1
echo [%DATE% %TIME%] Jarvis Memory Board exited with code %ERRORLEVEL% >> "%LOG%"
