"""NI-DAQmx boundary for NI 9422 pulse counting and NI 9264 AO output.

Hardware (NI MAX):
  Chassis : cDAQ-9178 -> cDAQ2
  Slot 1  : NI 9264   -> cDAQ2Mod1  (AO pump voltage, ao0, 0~5 V)
  Slot 2  : NI 9422   -> cDAQ2Mod2  (DI counter, first channel = DI0/PFI0)

Flow meter: Aichi OF05ZAT-AR (voltage pulse, no pull-up)
  White Out -> DI0+
  DI0- / sensor Black / AO GND -> common PSU GND

Pulse counting uses chassis counter cDAQ2/ctr0 with source terminal
/cDAQ2Mod2/PFI0 (NI 9422 Count Edges default for Ctr0 / DI0).
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass


CHASSIS = "cDAQ2"
AO_MODULE = "cDAQ2Mod1"  # NI 9264
DI_MODULE = "cDAQ2Mod2"  # NI 9422


@dataclass
class ChannelConfig:
    """First channel on each module."""

    ao_pump: str = f"{AO_MODULE}/ao0"
    counter: str = f"{CHASSIS}/ctr0"
    # Leading slash is required for DAQmx terminal names.
    counter_term: str = f"/{DI_MODULE}/PFI0"


class DaqService:
    """Hardware access with graceful simulation fallback."""

    REQUIRED_DEVICES = (AO_MODULE, DI_MODULE)

    def __init__(self, channels: ChannelConfig | None = None) -> None:
        self.channels = channels or ChannelConfig()
        self.available = False
        self.error = "NI-DAQmx를 확인하는 중입니다."
        self.device_summary = "NI 9264 / NI 9422 대기"
        self._nidaqmx = None
        self._counter_task = None
        self._last_count = 0
        self._last_count_at = time.monotonic()
        self._gate_count = 0
        self._gate_started_at = time.monotonic()
        self._last_hz = 0.0
        self._sim_phase = 0.0
        self._sim_count = 0
        # Low-frequency pulse meters (~4 Hz) need a longer gate than the UI poll.
        self.gate_seconds = 0.5
        try:
            import nidaqmx  # type: ignore

            self._nidaqmx = nidaqmx
        except ImportError:
            self.error = "nidaqmx 패키지가 없습니다 (시뮬레이션 모드)."

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
                self.device_summary = "NI MAX에서 cDAQ2Mod1 / cDAQ2Mod2 확인 필요"
                return False

            ao = devices[AO_MODULE]
            di = devices[DI_MODULE]
            self.available = True
            self.error = "연결됨"
            self.device_summary = (
                f"NI 9264 {AO_MODULE} / NI 9422 {DI_MODULE} "
                f"(S/N {getattr(ao, 'serial_num', '?')}/{getattr(di, 'serial_num', '?')})"
            )
        except Exception as exc:  # noqa: BLE001 - keep UI alive on driver errors
            self.available = False
            self.error = f"통신 오류: {exc}"
        return self.available

    def start_counter(self) -> None:
        self.stop_counter()
        now = time.monotonic()
        self._last_count_at = now
        self._gate_started_at = now
        self._gate_count = 0
        self._last_hz = 0.0
        self._sim_count = 0
        self._sim_phase = 0.0

        if not (self.available and self._nidaqmx):
            self._last_count = 0
            return

        try:
            from nidaqmx.constants import CountDirection, Edge  # type: ignore

            task = self._nidaqmx.Task()
            channel = task.ci_channels.add_ci_count_edges_chan(
                self.channels.counter,
                name_to_assign_to_channel="flow_pulses",
                edge=Edge.RISING,
                initial_count=0,
                count_direction=CountDirection.COUNT_UP,
            )
            channel.ci_count_edges_term = self.channels.counter_term
            task.start()
            self._counter_task = task
            self._last_count = int(task.read())
            self.error = "연결됨"
        except Exception as exc:  # noqa: BLE001
            self._counter_task = None
            self.error = f"카운터 시작 오류: {exc}"
            self._last_count = 0

    def stop_counter(self) -> None:
        task = self._counter_task
        self._counter_task = None
        if task is None:
            return
        try:
            task.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            task.close()
        except Exception:  # noqa: BLE001
            pass

    def read_pulse_rate(self) -> tuple[float, float, int]:
        """Return gated (frequency_hz, gate_elapsed_s, gate_pulses).

        Uses a rolling gate (~0.5 s) so ~4 Hz flow-meter pulses do not
        collapse to 0 Hz on short UI poll intervals.
        """
        now = time.monotonic()
        poll_elapsed = max(now - self._last_count_at, 1e-6)
        delta = 0

        if self._counter_task is not None:
            try:
                count = int(self._counter_task.read())
                delta = count - self._last_count
                if delta < 0:
                    delta = count  # unlikely wrap / reset
                self._last_count = count
                self._last_count_at = now
            except Exception as exc:  # noqa: BLE001
                self.error = f"카운터 읽기 오류: {exc}"
                return self._last_hz, max(now - self._gate_started_at, 1e-6), self._gate_count
        else:
            # Simulation ~120 cc/min with 0.46 ml/P => ~4.35 Hz
            target_hz = 4.35 + 0.25 * math.sin(now * 0.7) + random.uniform(-0.05, 0.05)
            expected = target_hz * poll_elapsed
            whole = int(expected)
            self._sim_phase += expected - whole
            if self._sim_phase >= 1.0:
                whole += 1
                self._sim_phase -= 1.0
            delta = whole
            self._sim_count += whole
            self._last_count = self._sim_count
            self._last_count_at = now

        self._gate_count += max(0, delta)
        gate_elapsed = max(now - self._gate_started_at, 1e-6)
        if gate_elapsed >= self.gate_seconds:
            self._last_hz = self._gate_count / gate_elapsed
            hz = self._last_hz
            pulses = self._gate_count
            elapsed = gate_elapsed
            self._gate_count = 0
            self._gate_started_at = now
            return hz, elapsed, pulses

        # Between gate boundaries keep the last stable frequency estimate.
        return self._last_hz, gate_elapsed, self._gate_count

    def write_voltage(self, voltage: float, channel: str | None = None) -> float:
        voltage = max(0.0, min(5.0, float(voltage)))
        channel = channel or self.channels.ao_pump
        if not (self.available and self._nidaqmx):
            return voltage
        try:
            with self._nidaqmx.Task() as task:
                task.ao_channels.add_ao_voltage_chan(channel, min_val=0.0, max_val=5.0)
                task.write(voltage, auto_start=True)
            self.error = "연결됨"
        except Exception as exc:  # noqa: BLE001
            self.error = f"AO 출력 오류: {exc}"
        return voltage

    def close(self) -> None:
        self.stop_counter()
        self.write_voltage(0.0)
