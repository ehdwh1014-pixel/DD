"""NI-DAQmx boundary for pump AO, level DI, and valve DO.

Hardware (NI MAX):
  Chassis : cDAQ-9178 -> cDAQ2
  Slot 1  : NI 9264   -> cDAQ2Mod1  (REF.W ao0 + NG PUMP ao1 + spare ao2)
  Slot 2  : NI 9422   -> cDAQ2Mod2  (three HIGH/LOW level inputs, DI0..DI5)
  Slot 3  : NI 9477   -> cDAQ2Mod3  (valves DO0..2, igniter DO3, FX DO4, spare DO5)

LS iG5A wiring (NPN / sink):
  9264 ao0 (REF.W) -> V1,  9264 AO COM -> CM
  9264 ao1 (NG PUMP) -> NG pump command 0~5 V
  9477 DO4  -> P1(FX), 9477 COM -> CM
  Measured NI 9923 screws: DO0..4 = #1..#5, COM = #9 (also 10/27/28)

Flow PV is read from Autonics MP5Y-25 over USB-RS485 (see mp5y_service.py).
"""

from __future__ import annotations

from dataclasses import dataclass


CHASSIS = "cDAQ2"
AO_MODULE = "cDAQ2Mod1"  # NI 9264
DI_MODULE = "cDAQ2Mod2"  # NI 9422
DO_MODULE = "cDAQ2Mod3"  # NI 9477


class DaqError(RuntimeError):
    """Raised when an expected hardware operation cannot be completed."""


@dataclass
class ChannelConfig:
    """DAQmx channels used by the control application."""

    ao_pump: str = f"{AO_MODULE}/ao0"
    ao_ng_pump: str = f"{AO_MODULE}/ao1"
    spare_ao: str = f"{AO_MODULE}/ao2"
    level_inputs: str = f"{DI_MODULE}/port0/line0:5"
    valve_outputs: str = f"{DO_MODULE}/port0/line0:2"
    # IFW15 flame detector (potential-free contacts) -> NI 9422 DI6 (line6).
    # SSR input -> NI 9477 DO3 (line3). NI 9477 is sinking output.
    flame_input: str = f"{DI_MODULE}/port0/line6"
    igniter_output: str = f"{DO_MODULE}/port0/line3"
    # LS iG5A RUN: DO4 sinks P1(FX) to COM when ON (NPN mode).
    inverter_run: str = f"{DO_MODULE}/port0/line4"
    spare_do: str = f"{DO_MODULE}/port0/line5"


class DaqService:
    """Analog output access with graceful simulation fallback."""

    REQUIRED_DEVICES = (AO_MODULE, DI_MODULE, DO_MODULE)

    def __init__(self, channels: ChannelConfig | None = None) -> None:
        self.channels = channels or ChannelConfig()
        self.available = False
        self.level_available = False
        self.error = "NI-DAQmx를 확인하는 중입니다."
        self.device_summary = "NI 9264 / 9422 / 9477 대기"
        self._nidaqmx = None
        self._ao_tasks: dict[str, object] = {}
        self._di_task = None
        self._do_task = None
        self._di_flame_task = None
        self._do_igniter_task = None
        self._do_inverter_task = None
        self._do_spare_task = None
        self._level_channel: str | None = None
        self._valve_channel: str | None = None
        self._flame_channel: str | None = None
        self._igniter_channel: str | None = None
        self._inverter_channel: str | None = None
        self._spare_do_channel: str | None = None
        self._last_valves = [False, False, False]
        self._last_igniter = False
        self._last_inverter = False
        self._last_spare_do = False
        try:
            import nidaqmx  # type: ignore

            self._nidaqmx = nidaqmx
        except ImportError:
            self.error = "nidaqmx 패키지 없음 (시뮬레이션). EXE 재빌드 또는 NI-DAQmx 확인."

    def check_connection(self) -> bool:
        if self._nidaqmx is None:
            self.available = False
            self.level_available = False
            return False
        try:
            devices = {
                device.name: device
                for device in self._nidaqmx.system.System.local().devices
            }
            found = sorted(devices)
            self.available = AO_MODULE in devices
            self.level_available = DI_MODULE in devices and DO_MODULE in devices
            missing = [name for name in self.REQUIRED_DEVICES if name not in devices]
            if not missing:
                self.error = "연결됨"
            elif found:
                self.error = (
                    f"장치명 불일치 · 필요 {', '.join(missing)} / "
                    f"PC감지 {', '.join(found)}"
                )
            else:
                self.error = "NI 장치 없음 · NI-DAQmx/케이블 확인"
            states = [
                f"9264 {'OK' if self.available else '없음'}",
                f"9422 {'OK' if DI_MODULE in devices else '없음'}",
                f"9477 {'OK' if DO_MODULE in devices else '없음'}",
            ]
            self.device_summary = " / ".join(states)
        except Exception as exc:  # noqa: BLE001 - keep UI alive on driver errors
            self.available = False
            self.level_available = False
            self.error = f"통신 오류: {exc}"
        return self.available and self.level_available

    def read_levels(self) -> list[bool]:
        """Read [HIGH1, LOW1, HIGH2, LOW2, HIGH3, LOW3]."""
        if not (self.level_available and self._nidaqmx):
            return [False] * 6
        try:
            if self._di_task is not None and self._level_channel != self.channels.level_inputs:
                self._close_di_task()
            if self._di_task is None:
                from nidaqmx.constants import LineGrouping  # type: ignore

                task = self._nidaqmx.Task()
                task.di_channels.add_di_chan(
                    self.channels.level_inputs,
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                self._di_task = task
                self._level_channel = self.channels.level_inputs
            values = self._di_task.read()
            if isinstance(values, bool):
                values = [values]
            result = [bool(value) for value in values]
            if len(result) != 6:
                raise DaqError(f"레벨 입력 개수 오류: {len(result)}")
            return result
        except Exception as exc:  # noqa: BLE001
            self._close_di_task()
            if isinstance(exc, DaqError):
                raise
            raise DaqError(f"레벨 입력 오류: {exc}") from exc

    def write_valves(self, opened: list[bool]) -> list[bool]:
        """Write DO0..DO2. True turns the 9477 sink ON (white SIG -> 0 V)."""
        if len(opened) != 3:
            raise ValueError("밸브 출력은 3개여야 합니다.")
        values = [bool(value) for value in opened]
        if not (self.level_available and self._nidaqmx):
            self._last_valves = values
            return values
        try:
            if self._do_task is not None and self._valve_channel != self.channels.valve_outputs:
                self._close_do_task()
            if self._do_task is None:
                from nidaqmx.constants import LineGrouping  # type: ignore

                task = self._nidaqmx.Task()
                task.do_channels.add_do_chan(
                    self.channels.valve_outputs,
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                self._do_task = task
                self._valve_channel = self.channels.valve_outputs
            self._do_task.write(values, auto_start=True)
            self._last_valves = values
            return values
        except Exception as exc:  # noqa: BLE001
            self._close_do_task()
            raise DaqError(f"밸브 출력 오류: {exc}") from exc

    def read_flame(self) -> bool:
        """Read IFW15 flame detector contact (DI6).

        Expected wiring:
          DI+ <- external +24V (sensor contact supply)
          DI- <- external 0V
          NO/NC contact closure will make DI ON depending on wiring.
        """
        if not (self.level_available and self._nidaqmx):
            return False
        try:
            if self._di_flame_task is not None and self._flame_channel != self.channels.flame_input:
                self._close_di_flame_task()
            if self._di_flame_task is None:
                from nidaqmx.constants import LineGrouping  # type: ignore

                task = self._nidaqmx.Task()
                task.di_channels.add_di_chan(
                    self.channels.flame_input,
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                self._di_flame_task = task
                self._flame_channel = self.channels.flame_input
            value = self._di_flame_task.read()
            # read() may return bool or list[bool] depending on grouping
            if isinstance(value, bool):
                return value
            if isinstance(value, (list, tuple)) and value:
                return bool(value[0])
            return bool(value)
        except Exception as exc:  # noqa: BLE001
            self._close_di_flame_task()
            raise DaqError(f"화염 DI 읽기 오류: {exc}") from exc

    def write_igniter(self, on: bool) -> bool:
        """Write SSR input control (DO3)."""
        on = bool(on)
        if not (self.level_available and self._nidaqmx):
            self._last_igniter = on
            return on
        try:
            if self._do_igniter_task is not None and self._igniter_channel != self.channels.igniter_output:
                self._close_do_igniter_task()
            if self._do_igniter_task is None:
                from nidaqmx.constants import LineGrouping  # type: ignore

                task = self._nidaqmx.Task()
                task.do_channels.add_do_chan(
                    self.channels.igniter_output,
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                self._do_igniter_task = task
                self._igniter_channel = self.channels.igniter_output
            self._do_igniter_task.write([on], auto_start=True)
            self._last_igniter = on
            return on
        except Exception as exc:  # noqa: BLE001
            self._close_do_igniter_task()
            raise DaqError(f"점화기 SSR 출력 오류: {exc}") from exc

    def write_inverter_run(self, on: bool) -> bool:
        """Write LS iG5A FX (P1) via DO4 sink to COM."""
        on = bool(on)
        if not (self.level_available and self._nidaqmx):
            self._last_inverter = on
            return on
        try:
            if (
                self._do_inverter_task is not None
                and self._inverter_channel != self.channels.inverter_run
            ):
                self._close_do_inverter_task()
            if self._do_inverter_task is None:
                from nidaqmx.constants import LineGrouping  # type: ignore

                task = self._nidaqmx.Task()
                task.do_channels.add_do_chan(
                    self.channels.inverter_run,
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                self._do_inverter_task = task
                self._inverter_channel = self.channels.inverter_run
            self._do_inverter_task.write([on], auto_start=True)
            self._last_inverter = on
            return on
        except Exception as exc:  # noqa: BLE001
            self._close_do_inverter_task()
            raise DaqError(f"인버터 RUN(DO4) 출력 오류: {exc}") from exc

    def write_spare_do(self, on: bool) -> bool:
        """Write the spare NI 9477 sinking output (default DO5)."""
        on = bool(on)
        if not (self.level_available and self._nidaqmx):
            self._last_spare_do = on
            return on
        try:
            if (
                self._do_spare_task is not None
                and self._spare_do_channel != self.channels.spare_do
            ):
                self._close_do_spare_task()
            if self._do_spare_task is None:
                from nidaqmx.constants import LineGrouping  # type: ignore

                task = self._nidaqmx.Task()
                task.do_channels.add_do_chan(
                    self.channels.spare_do,
                    line_grouping=LineGrouping.CHAN_PER_LINE,
                )
                self._do_spare_task = task
                self._spare_do_channel = self.channels.spare_do
            self._do_spare_task.write([on], auto_start=True)
            self._last_spare_do = on
            return on
        except Exception as exc:  # noqa: BLE001
            self._close_do_spare_task()
            raise DaqError(f"DO 5 출력 오류: {exc}") from exc

    def write_voltage(self, voltage: float, channel: str | None = None) -> float:
        voltage = max(0.0, min(5.0, float(voltage)))
        channel = channel or self.channels.ao_pump
        if not (self.available and self._nidaqmx):
            return voltage
        try:
            task = self._ao_tasks.get(channel)
            if task is None:
                task = self._nidaqmx.Task()
                task.ao_channels.add_ao_voltage_chan(channel, min_val=0.0, max_val=5.0)
                self._ao_tasks[channel] = task
            task.write(voltage, auto_start=True)
            self.error = "연결됨"
        except Exception as exc:  # noqa: BLE001
            self.error = f"AO 출력 오류: {exc}"
            self._close_ao_task(channel)
            raise DaqError(self.error) from exc
        return voltage

    def _close_ao_task(self, channel: str | None = None) -> None:
        channels = [channel] if channel is not None else list(self._ao_tasks)
        for task_channel in channels:
            task = self._ao_tasks.pop(task_channel, None)
            if task is None:
                continue
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def _close_di_task(self) -> None:
        task = self._di_task
        self._di_task = None
        self._level_channel = None
        if task is not None:
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def _close_di_flame_task(self) -> None:
        task = self._di_flame_task
        self._di_flame_task = None
        self._flame_channel = None
        if task is not None:
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def _close_do_task(self) -> None:
        task = self._do_task
        self._do_task = None
        self._valve_channel = None
        if task is not None:
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def _close_do_igniter_task(self) -> None:
        task = self._do_igniter_task
        self._do_igniter_task = None
        self._igniter_channel = None
        if task is not None:
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def _close_do_inverter_task(self) -> None:
        task = self._do_inverter_task
        self._do_inverter_task = None
        self._inverter_channel = None
        if task is not None:
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def _close_do_spare_task(self) -> None:
        task = self._do_spare_task
        self._do_spare_task = None
        self._spare_do_channel = None
        if task is not None:
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        try:
            self.write_valves([False, False, False])
        except DaqError:
            pass
        try:
            self.write_igniter(False)
        except DaqError:
            pass
        try:
            self.write_inverter_run(False)
        except DaqError:
            pass
        try:
            self.write_spare_do(False)
        except DaqError:
            pass
        try:
            self.write_voltage(0.0)
        except DaqError:
            pass
        try:
            self.write_voltage(0.0, self.channels.ao_ng_pump)
        except DaqError:
            pass
        try:
            self.write_voltage(0.0, self.channels.spare_ao)
        except DaqError:
            pass
        self._close_di_task()
        self._close_do_task()
        self._close_di_flame_task()
        self._close_do_igniter_task()
        self._close_do_inverter_task()
        self._close_do_spare_task()
        self._close_ao_task()
