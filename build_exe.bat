@echo off
REM Build a single-file Windows exe with the app icon.
REM Run from the project root in PowerShell or cmd.
REM Industrial PCs still need NI-DAQmx Runtime installed separately.

py -m pip install -r requirements.txt pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
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

echo.
echo Done. Output: dist\PulseFlow.exe
echo Note: Install NI-DAQmx Runtime on the industrial PC separately.
pause
