"""PyInstaller hook: bundle nidaqmx metadata and submodules for one-file EXE."""

from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = collect_all("nidaqmx")
datas += copy_metadata("nidaqmx")
