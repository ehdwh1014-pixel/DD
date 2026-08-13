"""PyInstaller hook: bundle only needed nidaqmx metadata/submodules."""

from importlib.metadata import PackageNotFoundError

from PyInstaller.utils.hooks import (
    collect_submodules,
    copy_metadata,
)

# Prefer submodule collection over collect_all to avoid shipping tests/docs.
hiddenimports = collect_submodules("nidaqmx")
datas = []
binaries = []

for package in (
    "nidaqmx",
    "nitypes",
    "hightime",
    "tzlocal",
    "python-decouple",
    "requests",
    "certifi",
    "idna",
    "urllib3",
    "charset-normalizer",
):
    try:
        datas += copy_metadata(package)
    except PackageNotFoundError:
        pass

# Drop test/doc packages if collect_submodules pulled them.
hiddenimports = [
    name
    for name in hiddenimports
    if ".tests" not in name and not name.endswith(".test")
]
