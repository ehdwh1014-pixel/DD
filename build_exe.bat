@echo off
cd /d "%~dp0"
echo PulseFlow EXE build
echo Folder: %CD%
echo.

where py >nul 2>&1
if %errorlevel%==0 (
  py -3 build_exe.py
  goto AFTER
)

where python >nul 2>&1
if %errorlevel%==0 (
  python build_exe.py
  goto AFTER
)

echo [ERROR] Python not found.
echo Install Python 3 64-bit and check Add python.exe to PATH.
pause
exit /b 1

:AFTER
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed.
  pause
  exit /b 1
)

echo.
pause
