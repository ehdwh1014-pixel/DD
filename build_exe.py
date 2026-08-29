"""Build PulseFlow.exe for Windows industrial PC deployment.

Uses a ctypes NI-DAQmx wrapper (no Python nidaqmx/numpy), so the one-file
EXE stays small and starts faster on industrial PCs.
"""

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

EXCLUDE_MODULES = (
    "nidaqmx",
    "numpy",
    "matplotlib",
    "scipy",
    "pandas",
    "PIL",
    "cv2",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "sphinx",
    "grpc",
    "grpcio",
    "aiohttp",
    "tornado",
    "requests",
    "pymodbus",
    "tkinter.test",
    "unittest",
    "pydoc",
    "doctest",
)


def run(command: list[str]) -> None:
    print(">", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def pyinstaller_command() -> list[str]:
    command = [
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
        "--hidden-import",
        "serial",
        "--hidden-import",
        "serial.tools.list_ports",
        "--hidden-import",
        "flow_ui.nidaqmx_lite",
        "--hidden-import",
        "flow_ui.modbus_rtu",
        str(ROOT / "pump.py"),
    ]
    for module in EXCLUDE_MODULES:
        command.extend(["--exclude-module", module])
    return command


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
    run(pyinstaller_command())

    exe_path = DIST / EXE_NAME
    if not exe_path.exists():
        print(f"[ERROR] Missing {exe_path}")
        return 1

    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"EXE size: {size_mb:.1f} MB")

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
    print(f"SIZE: {size_mb:.1f} MB")
    print()
    print("Notes:")
    print("- EXE no longer embeds Python nidaqmx/numpy (uses NI Runtime DLL)")
    print("- Industrial PC still needs NI-DAQmx Runtime installed")
    print("- Prefer C:\\PulseFlow over OneDrive/Desktop for faster start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
