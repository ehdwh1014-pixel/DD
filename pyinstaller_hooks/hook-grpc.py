"""PyInstaller hook: optional grpc bundle.

Some Python environments do not install grpc/grpcio. Keep this hook
best-effort so PyInstaller build does not fail when grpc is absent.
"""

from importlib.metadata import PackageNotFoundError

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

try:
    datas = collect_data_files("grpc")
except Exception:  # noqa: BLE001 - optional dependency
    datas = []

try:
    datas += copy_metadata("grpcio")
except PackageNotFoundError:
    pass
