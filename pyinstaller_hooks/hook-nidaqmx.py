"""PyInstaller hook: bundle nidaqmx + dependency metadata for one-file EXE."""

from importlib.metadata import PackageNotFoundError

from PyInstaller.utils.hooks import collect_all, copy_metadata

datas, binaries, hiddenimports = collect_all("nidaqmx")

# nidaqmx (and some deps such as nitypes/hightime) call importlib.metadata
# at runtime, so metadata must be bundled in one-file builds.
for package in (
    "nidaqmx",
    "nitypes",
    "hightime",
    "tzlocal",
    "python-decouple",
    "requests",
):
    try:
        datas += copy_metadata(package)
    except PackageNotFoundError:
        pass
