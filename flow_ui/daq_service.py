"""NI-DAQmx boundary for NI 9264 AO pump output.

Hardware (NI MAX):
  Chassis : cDAQ-9178 -> cDAQ2
  Slot 1  : NI 9264   -> cDAQ2Mod1  (AO pump voltage, ao0, 0~5 V)

Flow PV is read from Autonics MP5Y-25 over USB-RS485 (see mp5y_service.py).
"""

from __future__ import annotations

from dataclasses import dataclass


CHASSIS = "cDAQ2"
AO_MODULE = "cDAQ2Mod1"  # NI 9264


class DaqError(RuntimeError):
    """Raised when an expected hardware operation cannot be completed."""


@dataclass
class ChannelConfig:
    """First AO channel on the 9264 module."""

    ao_pump: str = f"{AO_MODULE}/ao0"


class DaqService:
    """Analog output access with graceful simulation fallback."""

    REQUIRED_DEVICES = (AO_MODULE,)

    def __init__(self, channels: ChannelConfig | None = None) -> None:
        self.channels = channels or ChannelConfig()
        self.available = False
        self.error = "NI-DAQmx를 확인하는 중입니다."
        self.device_summary = "NI 9264 대기"
        self._nidaqmx = None
        self._ao_task = None
        self._ao_channel: str | None = None
        try:
            import nidaqmx  # type: ignore

            self._nidaqmx = nidaqmx
        except ImportError:
            self.error = "nidaqmx 패키지가 없습니다 (AO 시뮬레이션)."

    def check_connection(self) -> bool:
        if self._nidaqmx is None:
            self.available = False
            return False
        try:
            devices = {
                device.name: device
                for device in self._nidaqmx.system.System.local().devices
            }
            missing = [name for name in self.REQUIRED_DEVICES if name not in devices]
            if missing:
                self.available = False
                self.error = f"장치 없음: {', '.join(missing)}"
                self.device_summary = "NI MAX에서 cDAQ2Mod1 확인 필요"
                return False

            ao = devices[AO_MODULE]
            self.available = True
            self.error = "연결됨"
            self.device_summary = (
                f"NI 9264 {AO_MODULE} (S/N {getattr(ao, 'serial_num', '?')})"
            )
        except Exception as exc:  # noqa: BLE001 - keep UI alive on driver errors
            self.available = False
            self.error = f"통신 오류: {exc}"
        return self.available

    def write_voltage(self, voltage: float, channel: str | None = None) -> float:
        voltage = max(0.0, min(5.0, float(voltage)))
        channel = channel or self.channels.ao_pump
        if not (self.available and self._nidaqmx):
            return voltage
        try:
            if self._ao_task is not None and self._ao_channel != channel:
                self._close_ao_task()
            if self._ao_task is None:
                task = self._nidaqmx.Task()
                task.ao_channels.add_ao_voltage_chan(channel, min_val=0.0, max_val=5.0)
                self._ao_task = task
                self._ao_channel = channel
            self._ao_task.write(voltage, auto_start=True)
            self.error = "연결됨"
        except Exception as exc:  # noqa: BLE001
            self.error = f"AO 출력 오류: {exc}"
            self._close_ao_task()
            raise DaqError(self.error) from exc
        return voltage

    def _close_ao_task(self) -> None:
        task = self._ao_task
        self._ao_task = None
        self._ao_channel = None
        if task is not None:
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        try:
            self.write_voltage(0.0)
        except DaqError:
            pass
        self._close_ao_task()
