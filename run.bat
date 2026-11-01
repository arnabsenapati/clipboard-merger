@echo off
setlocal

set "APP_DIR=%~dp0"
set "ROOT=%APP_DIR%.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

if exist "%PYTHON%" (
    "%PYTHON%" "%APP_DIR%app.py"
) else (
    python "%APP_DIR%app.py"
)
