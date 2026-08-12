"""PyInstaller hook: bundle grpc data files used by modern nidaqmx."""

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = collect_data_files("grpc")
datas += copy_metadata("grpcio")
