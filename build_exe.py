"""Build PulseFlow.exe for Windows industrial PC deployment."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DEPLOY = ROOT / "deploy"
EXE_NAME = "PulseFlow.exe"
ZIP_NAME = "PulseFlow_deploy.zip"


def run(command: list[str]) -> None:
    print(">", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    print(f"Folder: {ROOT}")
    print(f"Python: {sys.executable}")
    print()

    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            "requirements.txt",
            "pyinstaller",
        ]
    )

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            "PulseFlow",
            "--icon",
            str(ROOT / "flow_ui" / "assets" / "app_icon.ico"),
            "--add-data",
            f"{ROOT / 'flow_ui' / 'assets' / 'app_icon.ico'};flow_ui/assets",
            "--add-data",
            f"{ROOT / 'flow_ui' / 'assets' / 'app_icon.png'};flow_ui/assets",
            "--collect-all",
            "nidaqmx",
            "--collect-all",
            "pymodbus",
            "--hidden-import",
            "nidaqmx",
            "--hidden-import",
            "nidaqmx.system",
            "--hidden-import",
            "nidaqmx.constants",
            "--hidden-import",
            "nidaqmx.stream_writers",
            "--hidden-import",
            "nidaqmx.stream_readers",
            "--hidden-import",
            "pymodbus",
            "--hidden-import",
            "pymodbus.client",
            "--hidden-import",
            "serial",
            "pump.py",
        ]
    )

    exe_path = DIST / EXE_NAME
    if not exe_path.exists():
        print(f"[ERROR] Missing {exe_path}")
        return 1

    if DEPLOY.exists():
        shutil.rmtree(DEPLOY)
    DEPLOY.mkdir(parents=True)

    shutil.copy2(exe_path, DEPLOY / EXE_NAME)
    for name in ("README.md", "DEPLOY_PC.txt", "산업용PC_배포안내.txt"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, DEPLOY / source.name)

    zip_path = ROOT / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in DEPLOY.iterdir():
            archive.write(path, arcname=path.name)

    print()
    print("BUILD OK")
    print(f"EXE : {exe_path}")
    print(f"ZIP : {zip_path}")
    print("Copy the ZIP or EXE to the industrial PC.")
    print("Industrial PC still needs NI-DAQmx Runtime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
