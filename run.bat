@echo off
setlocal
cd /d "%~dp0"

if not "%~1"=="" goto :check_python
if exist "%~dp0PrismFX.exe" goto :launch_exe
goto :check_python

:launch_exe
start "" "%~dp0PrismFX.exe"
exit /b 0

:check_python
where python >nul 2>&1
if errorlevel 1 goto :no_python

python -c "import PySide6" >nul 2>&1
if errorlevel 1 goto :no_pyside6

if not "%~1"=="" goto :run_cli

where pythonw >nul 2>&1
if errorlevel 1 goto :run_gui_python
start "" pythonw "%~dp0main.py"
exit /b 0

:run_gui_python
start "" python "%~dp0main.py"
exit /b 0

:run_cli
python "%~dp0main.py" %*
exit /b %errorlevel%

:no_python
echo.
echo [PrismFX] Python 3.10+ was not found on PATH.
echo Please install Python 3.10 or newer from https://www.python.org/downloads/
echo and make sure "Add Python to PATH" is checked during installation.
echo.
pause
exit /b 1

:no_pyside6
echo.
echo [PrismFX] Missing required dependency: PySide6.
echo Please install it by running:
echo     pip install PySide6
echo.
pause
exit /b 1
