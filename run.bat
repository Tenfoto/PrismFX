@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
    if exist "%~dp0PrismFX.exe" (
        start "" "%~dp0PrismFX.exe"
    ) else (
        start "" pythonw "%~dp0main.py"
    )
    exit /b 0
)
python main.py %*
