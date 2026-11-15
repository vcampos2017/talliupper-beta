@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Only one TalliUpper should run at a time. A second copy can share port 8501
rem (one on IPv4, one on IPv6), so a browser may reach the wrong copy.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue) { exit 1 }" >nul 2>nul
if errorlevel 1 (
    echo TalliUpper already seems to be running on port 8501.
    echo Open http://localhost:8501 in your browser to use it.
    echo To restart it, close the other TalliUpper window first, then run this file again.
    pause
    exit /b 0
)

where py >nul 2>nul
if errorlevel 1 (
    echo The Python launcher was not found.
    echo Install Python 3.12 with this PowerShell command:
    echo winget install --id Python.Python.3.12 --exact --source winget
    pause
    exit /b 1
)

set "PYTHON_COMMAND="
py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PYTHON_COMMAND=py -3.12"

if not defined PYTHON_COMMAND (
    py -3.11 --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_COMMAND=py -3.11"
)

if not defined PYTHON_COMMAND (
    echo TalliUpper requires Python 3.11 or 3.12.
    echo Newer versions, including Python 3.14, are not yet supported by PaddlePaddle.
    echo.
    echo Install Python 3.12 with this PowerShell command:
    echo winget install --id Python.Python.3.12 --exact --source winget
    echo You may leave other Python versions installed.
    pause
    exit /b 1
)

for /f "delims=" %%V in ('%PYTHON_COMMAND% --version 2^>^&1') do set "PYTHON_VERSION=%%V"
echo Using %PYTHON_VERSION% for TalliUpper.

set "REBUILD_ENVIRONMENT="
if exist ".venv" if not exist ".venv\Scripts\python.exe" set "REBUILD_ENVIRONMENT=1"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else 1)" >nul 2>nul
    if errorlevel 1 set "REBUILD_ENVIRONMENT=1"
)

if defined REBUILD_ENVIRONMENT (
    echo.
    echo The existing TalliUpper environment is incomplete or uses an unsupported Python version.
    choice /C YN /M "Delete and rebuild only the project-local .venv folder"
    if errorlevel 2 exit /b 1
    rmdir /s /q ".venv"
    if exist ".venv" goto :error
)

if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: creating the Python environment...
    %PYTHON_COMMAND% -m venv .venv
    if errorlevel 1 goto :error
)

rem The ready marker holds a copy of the requirements files from the last
rem install, so an update that adds or changes a package reinstalls once.
copy /b /y requirements.txt + requirements-local.txt + requirements-core.txt ".venv\.requirements_snapshot" >nul
if errorlevel 1 goto :error

if exist ".venv\.receipt_lens_ready" (
    fc /b ".venv\.requirements_snapshot" ".venv\.receipt_lens_ready" >nul 2>nul
    if errorlevel 1 (
        echo TalliUpper's package list changed since the last install. Updating packages...
        del ".venv\.receipt_lens_ready"
    )
)

if exist ".venv\.receipt_lens_ready" (
    ".venv\Scripts\python.exe" -c "import paddle, paddleocr, pypdfium2, pillow_heif, reportlab, streamlit" >nul 2>nul
    if errorlevel 1 del ".venv\.receipt_lens_ready"
)

if not exist ".venv\.receipt_lens_ready" (
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :error
    echo Installing TalliUpper and the local OCR engine. This can take several minutes...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
    copy /b /y ".venv\.requirements_snapshot" ".venv\.receipt_lens_ready" >nul
)

echo Starting TalliUpper...
".venv\Scripts\python.exe" -m streamlit run app.py
if errorlevel 1 goto :error
exit /b 0

:error
echo Setup did not finish. Review the error above, then run this file again.
pause
exit /b 1
