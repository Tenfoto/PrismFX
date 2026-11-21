@echo off
setlocal
cd /d "%~dp0"
set PROFILE_DIR=%TEMP%\prismfx_verify_run
if exist "%PROFILE_DIR%" rmdir /s /q "%PROFILE_DIR%"

echo [1/5] Generating procedural icons...
python tools\make_icon.py || exit /b 1

echo [2/5] Running --selftest (core, physics, recovery journal, 42 GPU shaders)...
python main.py --selftest || exit /b 1

echo [3/5] Running --uitest (non-intrusive hit-test ^& Thai font verification)...
python main.py --profile "%PROFILE_DIR%" --uitest || exit /b 1

echo [4/5] Running --overlay-test (Win32 click-through ^& alpha compositing)...
python main.py --profile "%PROFILE_DIR%" --overlay-test || exit /b 1

echo [5/5] Running --perf-log (165 Hz GPU frame pacing benchmark)...
python main.py --profile "%PROFILE_DIR%" --perf-log 1.5 || exit /b 1

echo.
echo ALL PRISMFX VERIFICATION SUITES PASSED!
