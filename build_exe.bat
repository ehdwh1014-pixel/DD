@echo off
REM Build a single-file Windows exe with the app icon.
REM Run from the project root in PowerShell or cmd.

py -m pip install -r requirements.txt pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "PulseFlow" ^
  --icon "flow_ui\assets\app_icon.ico" ^
  --add-data "flow_ui\assets\app_icon.ico;flow_ui\assets" ^
  --add-data "flow_ui\assets\app_icon.png;flow_ui\assets" ^
  pump.py

echo.
echo Done. Output: dist\PulseFlow.exe
pause
