@echo off
setlocal

set "APP_DIR=%~dp0"
set "ROOT=%APP_DIR%.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

if exist "%PYTHON%" (
    "%PYTHON%" "%APP_DIR%build_single_exe.py"
) else (
    python "%APP_DIR%build_single_exe.py"
)
