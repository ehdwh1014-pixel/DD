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

# Heavy / unused modules that inflate onefile size and slow cold start.
EXCLUDE_MODULES = (
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
    "numpy.tests",
    "numpy.f2py",
    "numpy.distutils",
    "nidaqmx.tests",
    "pymodbus.server",
    "pymodbus.simulator",
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
        "nitypes",
        "--copy-metadata",
        "hightime",
        "--collect-submodules",
        "nidaqmx",
        "--collect-submodules",
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
        "nitypes",
        "--hidden-import",
        "hightime",
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

    verify_nidaqmx_import()
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
    print("Industrial PC checklist:")
    print("- Install NI-DAQmx Runtime 64-bit (MAX alone is not enough)")
    print("- Copy the NEW PulseFlow.exe (not an old one)")
    print("- Prefer local disk (C:\\PulseFlow) over OneDrive/Desktop for faster start")
    print("- MP5Y COM default is COM9; change in Settings if needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
