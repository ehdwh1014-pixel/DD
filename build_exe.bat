@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo PulseFlow EXE build
echo Folder: %CD%
echo ========================================
echo.

set "PYCMD="
where py >nul 2>&1 && set "PYCMD=py"
if not defined PYCMD (
  where python >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo [ERROR] Python not found.
  echo Install Python 3 64-bit from https://www.python.org/downloads/windows/
  echo Check "Add python.exe to PATH" during install, then reopen this folder and run again.
  echo.
  pause
  exit /b 1
)

echo Using: %PYCMD%
echo.
echo [1/3] Installing packages...
%PYCMD% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
  echo.
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

echo.
echo [2/3] Building EXE with PyInstaller...
%PYCMD% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "PulseFlow" ^
  --icon "flow_ui\assets\app_icon.ico" ^
  --add-data "flow_ui\assets\app_icon.ico;flow_ui\assets" ^
  --add-data "flow_ui\assets\app_icon.png;flow_ui\assets" ^
  --collect-all nidaqmx ^
  --collect-all pymodbus ^
  --hidden-import nidaqmx ^
  --hidden-import nidaqmx.system ^
  --hidden-import nidaqmx.constants ^
  --hidden-import nidaqmx.stream_writers ^
  --hidden-import nidaqmx.stream_readers ^
  --hidden-import pymodbus ^
  --hidden-import pymodbus.client ^
  --hidden-import serial ^
  pump.py
if errorlevel 1 (
  echo.
  echo [ERROR] PyInstaller build failed.
  pause
  exit /b 1
)

if not exist "dist\PulseFlow.exe" (
  echo.
  echo [ERROR] dist\PulseFlow.exe was not created.
  pause
  exit /b 1
)

echo.
echo [3/3] Creating deploy ZIP...
if exist deploy rmdir /s /q deploy
mkdir deploy
copy /y "dist\PulseFlow.exe" "deploy\PulseFlow.exe" >nul
copy /y "README.md" "deploy\README.md" >nul
if exist "DEPLOY_PC.txt" copy /y "DEPLOY_PC.txt" "deploy\DEPLOY_PC.txt" >nul
if exist "산업용PC_배포안내.txt" copy /y "산업용PC_배포안내.txt" "deploy\산업용PC_배포안내.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Path 'deploy\*' -DestinationPath 'PulseFlow_deploy.zip' -Force"
if errorlevel 1 (
  echo.
  echo [WARN] EXE was created, but ZIP creation failed.
  echo EXE path: %CD%\dist\PulseFlow.exe
  pause
  exit /b 1
)

echo.
echo ========================================
echo BUILD OK
echo EXE : %CD%\dist\PulseFlow.exe
echo ZIP : %CD%\PulseFlow_deploy.zip
echo ========================================
echo.
echo Copy PulseFlow_deploy.zip or dist\PulseFlow.exe to the industrial PC.
echo Industrial PC still needs NI-DAQmx Runtime.
echo.
pause
endlocal
