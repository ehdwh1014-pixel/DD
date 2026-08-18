"""Minimal NI-DAQmx ctypes wrapper (no numpy / no nidaqmx package).

Uses the system NI-DAQmx Runtime DLL (nicaiu.dll) for only the AO/DI/DO/TC
operations required by PulseFlow. This keeps the frozen EXE much smaller.
"""

from __future__ import annotations

import os
import re
import sys
import ctypes
from ctypes import (
    POINTER,
    byref,
    c_char_p,
    c_double,
    c_int32,
    c_uint32,
    c_uint8,
    c_void_p,
)
from dataclasses import dataclass


# NI-DAQmx TaskHandle is a pointer-sized opaque handle on 64-bit Windows.
TaskHandle = c_void_p
bool32 = c_uint32
float64 = c_double
int32 = c_int32
uInt32 = c_uint32
uInt8 = c_uint8

# Selected constants from NIDAQmx.h (must match NI-DAQmx, not zero!)
DAQmx_Val_Volts = 10348
DAQmx_Val_DegC = 10143
DAQmx_Val_ChanPerLine = 1030
DAQmx_Val_ChanForAllLines = 1031
DAQmx_Val_GroupByScanNumber = 1040
DAQmx_Val_GroupByChannel = 1041
DAQmx_Val_ThermocoupleType_K = 10073
DAQmx_Val_ThermocoupleType_T = 10086
DAQmx_Val_BuiltIn = 10200


class DaqmxLiteError(RuntimeError):
    """Raised when a NI-DAQmx C API call fails."""


def simulation_enabled() -> bool:
    return os.environ.get("PULSEFLOW_SIM", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def expand_channel_lines(spec: str) -> list[str]:
    """Expand `.../line0:5` or `.../ai0:4` into individual channel names."""
    spec = (spec or "").strip()
    if not spec:
        return []
    last = spec.rsplit("/", 1)[-1]
    if ":" not in last:
        return [spec]
    head, end_text = spec.rsplit(":", 1)
    match = re.match(r"^(.*?)(\d+)$", head)
    if not match:
        return [spec]
    try:
        end = int(end_text)
    except ValueError:
        return [spec]
    prefix, start_text = match.group(1), match.group(2)
    start = int(start_text)
    if end < start:
        start, end = end, start
    return [f"{prefix}{index}" for index in range(start, end + 1)]


@dataclass
class DeviceInfo:
    name: str
    product_type: str


def _load_dll() -> ctypes.CDLL:
    if sys.platform != "win32":
        raise DaqmxLiteError("Windows 전용 NI-DAQmx Runtime")
    try:
        return ctypes.WinDLL("nicaiu")
    except OSError as exc:
        raise DaqmxLiteError(
            "NI-DAQmx Runtime DLL(nicaiu) 없음. NI-DAQmx Runtime 설치 필요."
        ) from exc


class DaqmxLite:
    """Thin ctypes facade around nicaiu.dll."""

    def __init__(self) -> None:
        self._dll = _load_dll()
        self._started_tasks: set[int] = set()
        self._configure()

    def _configure(self) -> None:
        dll = self._dll

        dll.DAQmxGetExtendedErrorInfo.argtypes = [c_char_p, uInt32]
        dll.DAQmxGetExtendedErrorInfo.restype = int32

        dll.DAQmxGetSysDevNames.argtypes = [c_char_p, uInt32]
        dll.DAQmxGetSysDevNames.restype = int32

        dll.DAQmxGetDevProductType.argtypes = [c_char_p, c_char_p, uInt32]
        dll.DAQmxGetDevProductType.restype = int32

        dll.DAQmxCreateTask.argtypes = [c_char_p, POINTER(TaskHandle)]
        dll.DAQmxCreateTask.restype = int32
        dll.DAQmxClearTask.argtypes = [TaskHandle]
        dll.DAQmxClearTask.restype = int32
        dll.DAQmxStartTask.argtypes = [TaskHandle]
        dll.DAQmxStartTask.restype = int32
        dll.DAQmxStopTask.argtypes = [TaskHandle]
        dll.DAQmxStopTask.restype = int32

        dll.DAQmxCreateAOVoltageChan.argtypes = [
            TaskHandle,
            c_char_p,
            c_char_p,
            float64,
            float64,
            int32,
            c_char_p,
        ]
        dll.DAQmxCreateAOVoltageChan.restype = int32

        dll.DAQmxCreateDIChan.argtypes = [TaskHandle, c_char_p, c_char_p, int32]
        dll.DAQmxCreateDIChan.restype = int32
        dll.DAQmxCreateDOChan.argtypes = [TaskHandle, c_char_p, c_char_p, int32]
        dll.DAQmxCreateDOChan.restype = int32

        dll.DAQmxCreateAIThrmcplChan.argtypes = [
            TaskHandle,
            c_char_p,
            c_char_p,
            float64,
            float64,
            int32,
            int32,
            int32,
            float64,
            c_char_p,
        ]
        dll.DAQmxCreateAIThrmcplChan.restype = int32

        dll.DAQmxWriteAnalogScalarF64.argtypes = [
            TaskHandle,
            bool32,
            float64,
            float64,
            c_void_p,
        ]
        dll.DAQmxWriteAnalogScalarF64.restype = int32

        dll.DAQmxWriteDigitalLines.argtypes = [
            TaskHandle,
            int32,
            bool32,
            float64,
            bool32,
            POINTER(uInt8),
            POINTER(int32),
            c_void_p,
        ]
        dll.DAQmxWriteDigitalLines.restype = int32

        dll.DAQmxReadDigitalLines.argtypes = [
            TaskHandle,
            int32,
            float64,
            bool32,
            POINTER(uInt8),
            uInt32,
            POINTER(int32),
            POINTER(int32),
            c_void_p,
        ]
        dll.DAQmxReadDigitalLines.restype = int32

        dll.DAQmxReadAnalogF64.argtypes = [
            TaskHandle,
            int32,
            float64,
            bool32,
            POINTER(float64),
            uInt32,
            POINTER(int32),
            c_void_p,
        ]
        dll.DAQmxReadAnalogF64.restype = int32

    def _check(self, code: int) -> None:
        if int(code) >= 0:
            return
        buf = ctypes.create_string_buffer(2048)
        self._dll.DAQmxGetExtendedErrorInfo(buf, uInt32(2048))
        detail = buf.value.decode("utf-8", errors="replace").strip()
        raise DaqmxLiteError(detail or f"DAQmx error {code}")

    def list_devices(self) -> dict[str, DeviceInfo]:
        buf = ctypes.create_string_buffer(4096)
        self._check(self._dll.DAQmxGetSysDevNames(buf, uInt32(4096)))
        names = [
            part.strip()
            for part in buf.value.decode("utf-8", errors="replace").split(",")
            if part.strip()
        ]
        devices: dict[str, DeviceInfo] = {}
        for name in names:
            product = self.get_product_type(name)
            devices[name] = DeviceInfo(name=name, product_type=product)
        return devices

    def get_product_type(self, device_name: str) -> str:
        buf = ctypes.create_string_buffer(256)
        code = self._dll.DAQmxGetDevProductType(
            device_name.encode("utf-8"), buf, uInt32(256)
        )
        if int(code) < 0:
            return ""
        return buf.value.decode("utf-8", errors="replace")

    def create_task(self) -> TaskHandle:
        handle = TaskHandle(0)
        self._check(self._dll.DAQmxCreateTask(b"", byref(handle)))
        return handle

    def clear_task(self, handle: TaskHandle | None) -> None:
        if handle is None:
            return
        key = int(handle.value or 0)
        self._started_tasks.discard(key)
        try:
            self._dll.DAQmxClearTask(handle)
        except Exception:  # noqa: BLE001
            pass

    def create_ao_voltage(self, handle: TaskHandle, channel: str) -> None:
        self._check(
            self._dll.DAQmxCreateAOVoltageChan(
                handle,
                channel.encode("utf-8"),
                b"",
                float64(0.0),
                float64(5.0),
                int32(DAQmx_Val_Volts),
                None,
            )
        )

    def write_ao_voltage(self, handle: TaskHandle, voltage: float) -> None:
        self._check(
            self._dll.DAQmxWriteAnalogScalarF64(
                handle,
                bool32(1),
                float64(10.0),
                float64(voltage),
                None,
            )
        )

    def create_di_lines(self, handle: TaskHandle, lines: str) -> None:
        self._check(
            self._dll.DAQmxCreateDIChan(
                handle,
                lines.encode("utf-8"),
                b"",
                int32(DAQmx_Val_ChanPerLine),
            )
        )

    def create_do_lines(self, handle: TaskHandle, lines: str) -> None:
        self._check(
            self._dll.DAQmxCreateDOChan(
                handle,
                lines.encode("utf-8"),
                b"",
                int32(DAQmx_Val_ChanPerLine),
            )
        )

    def _ensure_task_started(self, handle: TaskHandle) -> None:
        key = int(handle.value or 0)
        if key in self._started_tasks:
            return
        self._check(self._dll.DAQmxStartTask(handle))
        self._started_tasks.add(key)

    def read_digital_lines(self, handle: TaskHandle, line_count: int) -> list[bool]:
        self._ensure_task_started(handle)
        data = (uInt8 * line_count)()
        samps = int32(0)
        bytes_per = int32(0)
        self._check(
            self._dll.DAQmxReadDigitalLines(
                handle,
                int32(1),
                float64(10.0),
                bool32(DAQmx_Val_GroupByChannel),
                data,
                uInt32(line_count),
                byref(samps),
                byref(bytes_per),
                None,
            )
        )
        return [bool(data[i]) for i in range(line_count)]

    def write_digital_lines(self, handle: TaskHandle, values: list[bool]) -> None:
        self._ensure_task_started(handle)
        data = (uInt8 * len(values))(*[1 if value else 0 for value in values])
        written = int32(0)
        self._check(
            self._dll.DAQmxWriteDigitalLines(
                handle,
                int32(1),
                bool32(1),
                float64(10.0),
                bool32(DAQmx_Val_GroupByChannel),
                data,
                byref(written),
                None,
            )
        )

    def create_ai_thermocouple(
        self,
        handle: TaskHandle,
        channels: str,
        *,
        tc_type: int,
        min_val: float,
        max_val: float,
    ) -> None:
        self._check(
            self._dll.DAQmxCreateAIThrmcplChan(
                handle,
                channels.encode("utf-8"),
                b"",
                float64(min_val),
                float64(max_val),
                int32(DAQmx_Val_DegC),
                int32(tc_type),
                int32(DAQmx_Val_BuiltIn),
                float64(25.0),
                None,
            )
        )

    def read_analog(self, handle: TaskHandle, channel_count: int) -> list[float]:
        data = (float64 * channel_count)()
        read = int32(0)
        self._check(
            self._dll.DAQmxReadAnalogF64(
                handle,
                int32(1),
                float64(10.0),
                bool32(DAQmx_Val_GroupByChannel),
                data,
                uInt32(channel_count),
                byref(read),
                None,
            )
        )
        return [float(data[i]) for i in range(channel_count)]


class FakeTask:
    """In-memory NI task used by FakeDaqmx."""

    def __init__(self, ident: int) -> None:
        self.value = ident
        self.kind = ""
        self.channel = ""


class FakeDaqmx:
    """Software stand-in for DaqmxLite so UI/control can be tested without hardware."""

    def __init__(self) -> None:
        self.devices = {
            "cDAQ2": DeviceInfo("cDAQ2", "cDAQ-9178"),
            "cDAQ2Mod1": DeviceInfo("cDAQ2Mod1", "NI 9264"),
            "cDAQ2Mod2": DeviceInfo("cDAQ2Mod2", "NI 9422"),
            "cDAQ2Mod3": DeviceInfo("cDAQ2Mod3", "NI 9477"),
            "cDAQ2Mod4": DeviceInfo("cDAQ2Mod4", "NI 9214"),
        }
        self.ao: dict[str, float] = {}
        self.do: dict[str, bool] = {}
        self.di: dict[str, bool] = {}
        self.tc = [25.0] * 10
        self._next_id = 1
        self._tasks: dict[int, FakeTask] = {}

    def list_devices(self) -> dict[str, DeviceInfo]:
        return dict(self.devices)

    def get_product_type(self, device_name: str) -> str:
        info = self.devices.get(device_name)
        return info.product_type if info else ""

    def create_task(self) -> FakeTask:
        task = FakeTask(self._next_id)
        self._next_id += 1
        self._tasks[task.value] = task
        return task

    def clear_task(self, handle: FakeTask | None) -> None:
        if handle is None:
            return
        self._tasks.pop(int(handle.value), None)

    def _task(self, handle: FakeTask) -> FakeTask:
        return self._tasks.setdefault(int(handle.value), handle)

    def create_ao_voltage(self, handle: FakeTask, channel: str) -> None:
        task = self._task(handle)
        task.kind = "ao"
        task.channel = channel
        self.ao.setdefault(channel, 0.0)

    def write_ao_voltage(self, handle: FakeTask, voltage: float) -> None:
        task = self._task(handle)
        channel = task.channel or "ao"
        self.ao[channel] = float(voltage)

    def create_di_lines(self, handle: FakeTask, lines: str) -> None:
        task = self._task(handle)
        task.kind = "di"
        task.channel = lines
        for name in expand_channel_lines(lines):
            self.di.setdefault(name, False)

    def create_do_lines(self, handle: FakeTask, lines: str) -> None:
        task = self._task(handle)
        task.kind = "do"
        task.channel = lines
        for name in expand_channel_lines(lines):
            self.do.setdefault(name, False)

    def read_digital_lines(self, handle: FakeTask, line_count: int) -> list[bool]:
        task = self._task(handle)
        names = expand_channel_lines(task.channel)
        values = [bool(self.di.get(name, False)) for name in names]
        if len(values) < line_count:
            values.extend([False] * (line_count - len(values)))
        return values[:line_count]

    def write_digital_lines(self, handle: FakeTask, values: list[bool]) -> None:
        task = self._task(handle)
        names = expand_channel_lines(task.channel)
        for name, value in zip(names, values):
            self.do[name] = bool(value)

    def create_ai_thermocouple(
        self,
        handle: FakeTask,
        channels: str,
        *,
        tc_type: int,
        min_val: float,
        max_val: float,
    ) -> None:
        task = self._task(handle)
        task.kind = "tc"
        existing = task.channel
        task.channel = channels if not existing else f"{existing},{channels}"

    def read_analog(self, handle: FakeTask, channel_count: int) -> list[float]:
        if len(self.tc) < channel_count:
            self.tc.extend([25.0] * (channel_count - len(self.tc)))
        return [float(value) for value in self.tc[:channel_count]]

    def set_di(self, channel: str, on: bool) -> None:
        self.di[channel] = bool(on)

    def valve_bits(self) -> list[bool]:
        return [
            bool(self.do.get("cDAQ2Mod3/port0/line0", False)),
            bool(self.do.get("cDAQ2Mod3/port0/line1", False)),
            bool(self.do.get("cDAQ2Mod3/port0/line2", False)),
        ]
