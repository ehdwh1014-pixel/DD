"""Build PulseFlow.exe for Windows industrial PC deployment."""

from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DEPLOY = ROOT / "deploy"
HOOKS = ROOT / "pyinstaller_hooks"
EXE_NAME = "PulseFlow.exe"
ZIP_NAME = "PulseFlow_deploy.zip"


def run(command: list[str]) -> None:
    print(">", " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def verify_nidaqmx_import() -> str:
    import nidaqmx  # noqa: WPS433 - build-time dependency check

    version = importlib.metadata.version("nidaqmx")
    print(f"nidaqmx import OK · version {version}")
    return version


def pyinstaller_command() -> list[str]:
    hooks_dir = HOOKS.resolve()
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
        "--additional-hooks-dir",
        str(hooks_dir),
        "--copy-metadata",
        "nidaqmx",
        "--copy-metadata",
        "grpcio",
        "--collect-all",
        "nidaqmx",
        "--collect-all",
        "grpc",
        "--collect-all",
        "pymodbus",
        "--hidden-import",
        "nidaqmx",
        "--hidden-import",
        "nidaqmx.system",
        "--hidden-import",
        "nidaqmx.constants",
        "--hidden-import",
        "nidaqmx.task",
        "--hidden-import",
        "nidaqmx.errors",
        "--hidden-import",
        "nidaqmx.stream_writers",
        "--hidden-import",
        "nidaqmx.stream_readers",
        "--hidden-import",
        "grpc",
        "--hidden-import",
        "pymodbus",
        "--hidden-import",
        "pymodbus.client",
        "--hidden-import",
        "serial",
        "--hidden-import",
        "serial.tools.list_ports",
        str(ROOT / "pump.py"),
    ]
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

    verify_nidaqmx_import()
    run(pyinstaller_command())

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
    print()
    print("Industrial PC checklist:")
    print("- Install NI-DAQmx Runtime 64-bit (MAX alone is not enough)")
    print("- Run the NEW PulseFlow.exe from this build")
    print("- If status still says 'nidaqmx 패키지 없음', rebuild on Windows 64-bit Python")
    print("- MP5Y COM port may differ from COM3 on a new PC (check Device Manager)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
