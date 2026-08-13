"""NI-DAQmx boundary for pump AO, level DI, and valve DO.

Hardware (NI MAX):
  Chassis : cDAQ-9178 -> cDAQ2
  Slot 1  : NI 9264   -> cDAQ2Mod1  (REF.W ao0 + NG PUMP ao1 + spare ao2)
  Slot 2  : NI 9422   -> cDAQ2Mod2  (three HIGH/LOW level inputs, DI0..DI5)
  Slot 3  : NI 9477   -> cDAQ2Mod3  (valves DO0..2, igniter DO3, spare DO4/DO5)
  Slot 4  : NI 9214   -> cDAQ2Mod4  (optional TC: K ai0..4, T ai5..9)

Uses a lightweight ctypes wrapper (flow_ui.nidaqmx_lite) so the frozen EXE
does not need the heavy Python nidaqmx/numpy packages.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from flow_ui.nidaqmx_lite import (
    DAQmx_Val_ThermocoupleType_K,
    DAQmx_Val_ThermocoupleType_T,
    DaqmxLite,
    DaqmxLiteError,
    DeviceInfo,
)


CHASSIS = "cDAQ2"
AO_MODULE = "cDAQ2Mod1"  # NI 9264
DI_MODULE = "cDAQ2Mod2"  # NI 9422
DO_MODULE = "cDAQ2Mod3"  # NI 9477
TC_MODULE = "cDAQ2Mod4"  # optional NI 9214

PRODUCT_AO = "9264"
PRODUCT_DI = "9422"
PRODUCT_DO = "9477"
PRODUCT_TC = "9214"


def device_from_channel(channel: str) -> str:
    return channel.split("/", 1)[0]


def product_matches(product_type: str, model: str) -> bool:
    return model in str(product_type or "").replace(" ", "").upper()


def discover_modules_by_product(devices: dict[str, object]) -> dict[str, str]:
    """Map AO/DI/DO/TC roles to NI MAX device names by module model."""
    roles = ("ao", "di", "do", "tc")
    models = (PRODUCT_AO, PRODUCT_DI, PRODUCT_DO, PRODUCT_TC)
    found: dict[str, str] = {}
    for device in devices.values():
        product = str(getattr(device, "product_type", "") or "")
        for role, model in zip(roles, models):
            if role in found:
                continue
            if product_matches(product, model):
                found[role] = str(getattr(device, "name", ""))
                break
    return found


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
    flame_input: str = f"{DI_MODULE}/port0/line6"
    igniter_output: str = f"{DO_MODULE}/port0/line3"
    # DO4 is a spare sink valve (same electrical style as DO0..2), not inverter FX.
    spare_do4: str = f"{DO_MODULE}/port0/line4"
    spare_do: str = f"{DO_MODULE}/port0/line5"
    # Optional inverter FX channel. Empty = unused (DO4 is free for a valve).
    inverter_run: str = ""
    tc_k_inputs: str = f"{TC_MODULE}/ai0:4"
    tc_t_inputs: str = f"{TC_MODULE}/ai5:9"


def apply_module_names(
    channels: ChannelConfig,
    *,
    ao: str | None = None,
    di: str | None = None,
    do: str | None = None,
    tc: str | None = None,
) -> None:
    if ao:
        channels.ao_pump = f"{ao}/ao0"
        channels.ao_ng_pump = f"{ao}/ao1"
        channels.spare_ao = f"{ao}/ao2"
    if di:
        channels.level_inputs = f"{di}/port0/line0:5"
        channels.flame_input = f"{di}/port0/line6"
    if do:
        channels.valve_outputs = f"{do}/port0/line0:2"
        channels.igniter_output = f"{do}/port0/line3"
        channels.spare_do4 = f"{do}/port0/line4"
        channels.spare_do = f"{do}/port0/line5"
    if tc:
        channels.tc_k_inputs = f"{tc}/ai0:4"
        channels.tc_t_inputs = f"{tc}/ai5:9"


class DaqService:
    """Analog/digital I/O access with graceful simulation fallback."""

    REQUIRED_DEVICES = (AO_MODULE, DI_MODULE, DO_MODULE)

    def __init__(self, channels: ChannelConfig | None = None) -> None:
        self.channels = channels or ChannelConfig()
        self.available = False
        self.level_available = False
        self.tc_available = False
        self.error = "NI-DAQmx를 확인하는 중입니다."
        self.device_summary = "NI 9264 / 9422 / 9477 대기"
        self._daq: DaqmxLite | None = None
        self._ao_tasks: dict[str, object] = {}
        self._di_task = None
        self._do_task = None
        self._di_flame_task = None
        self._do_igniter_task = None
        self._do_inverter_task = None
        self._do_spare4_task = None
        self._do_spare_task = None
        self._tc_task = None
        self._tc_channels: tuple[str, str] | None = None
        self._tc_lock = Lock()
        self._level_channel: str | None = None
        self._valve_channel: str | None = None
        self._flame_channel: str | None = None
        self._igniter_channel: str | None = None
        self._inverter_channel: str | None = None
        self._spare_do4_channel: str | None = None
        self._spare_do_channel: str | None = None
        self._last_valves = [False, False, False]
        self._last_igniter = False
        self._last_inverter = False
        self._last_spare_do4 = False
        self._last_spare_do = False
        try:
            self._daq = DaqmxLite()
        except DaqmxLiteError as exc:
            self.error = f"NI-DAQmx Runtime 없음 (시뮬레이션). {exc}"

    def _reset_io_tasks(self) -> None:
        self._close_di_task()
        self._close_do_task()
        self._close_di_flame_task()
        self._close_do_igniter_task()
        self._close_do_inverter_task()
        self._close_do_spare4_task()
        self._close_do_spare_task()
        self._close_tc_task()
        self._close_ao_task()

    def _try_auto_map_devices(self, devices: dict[str, DeviceInfo]) -> dict[str, str]:
        configured = {
            "ao": device_from_channel(self.channels.ao_pump),
            "di": device_from_channel(self.channels.level_inputs),
            "do": device_from_channel(self.channels.valve_outputs),
            "tc": device_from_channel(self.channels.tc_k_inputs),
        }
        discovered = discover_modules_by_product(devices)
        remap: dict[str, str] = {}
        for role in ("ao", "di", "do", "tc"):
            current = configured[role]
            if current in devices:
                continue
            candidate = discovered.get(role)
            if candidate and candidate != current:
                remap[role] = candidate
        if not remap:
            return {}
        apply_module_names(self.channels, **remap)
        self._reset_io_tasks()
        return remap

    def check_connection(self) -> bool:
        if self._daq is None:
            self.available = False
            self.level_available = False
            self.tc_available = False
            return False
        try:
            devices = self._daq.list_devices()
            found = sorted(devices)
            auto_map = self._try_auto_map_devices(devices)

            ao_device = device_from_channel(self.channels.ao_pump)
            di_device = device_from_channel(self.channels.level_inputs)
            do_device = device_from_channel(self.channels.valve_outputs)
            tc_device = device_from_channel(self.channels.tc_k_inputs)

            self.available = ao_device in devices
            self.level_available = di_device in devices and do_device in devices
            self.tc_available = tc_device in devices
            if not self.tc_available:
                self._close_tc_task()

            missing = [
                name
                for name, ok in (
                    (ao_device, self.available),
                    (di_device, di_device in devices),
                    (do_device, do_device in devices),
                )
                if not ok
            ]
            if self.available and self.level_available:
                if auto_map:
                    mapped = ", ".join(f"{role}={name}" for role, name in auto_map.items())
                    self.error = f"연결됨 · 자동매칭 {mapped}"
                else:
                    self.error = "연결됨"
            elif found:
                self.error = (
                    f"장치명 불일치 · 필요 {', '.join(missing)} / "
                    f"PC감지 {', '.join(found)} · 설정에서 채널명 수정 또는 "
                    "NI MAX에서 장치명 확인"
                )
            else:
                self.error = "NI 장치 없음 · NI-DAQmx/케이블 확인"
            states = [
                f"9264({ao_device}) {'OK' if self.available else '없음'}",
                f"9422({di_device}) {'OK' if di_device in devices else '없음'}",
                f"9477({do_device}) {'OK' if do_device in devices else '없음'}",
                f"9214({tc_device}) {'OK' if self.tc_available else '선택/없음'}",
            ]
            self.device_summary = " / ".join(states)
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.level_available = False
            self.tc_available = False
            self._close_tc_task()
            self.error = f"통신 오류: {exc}"
        return self.available and self.level_available

    def read_thermocouples(self) -> list[float]:
        if not (self.tc_available and self._daq):
            raise DaqError("NI 9214가 연결되지 않았습니다.")
        channels = (self.channels.tc_k_inputs, self.channels.tc_t_inputs)
        with self._tc_lock:
            try:
                if self._tc_task is not None and self._tc_channels != channels:
                    self._close_tc_task_unlocked()
                if self._tc_task is None:
                    task = self._daq.create_task()
                    self._daq.create_ai_thermocouple(
                        task,
                        self.channels.tc_k_inputs,
                        tc_type=DAQmx_Val_ThermocoupleType_K,
                        min_val=-200.0,
                        max_val=1200.0,
                    )
                    self._daq.create_ai_thermocouple(
                        task,
                        self.channels.tc_t_inputs,
                        tc_type=DAQmx_Val_ThermocoupleType_T,
                        min_val=-200.0,
                        max_val=400.0,
                    )
                    self._tc_task = task
                    self._tc_channels = channels
                values = self._daq.read_analog(self._tc_task, 10)
                if len(values) != 10:
                    raise DaqError(f"TC 입력 개수 오류: {len(values)} (필요 10)")
                return values
            except Exception as exc:  # noqa: BLE001
                self._close_tc_task_unlocked()
                if isinstance(exc, DaqError):
                    raise
                raise DaqError(f"NI 9214 TC 읽기 오류: {exc}") from exc

    def read_levels(self) -> list[bool]:
        if not (self.level_available and self._daq):
            return [False] * 6
        try:
            if self._di_task is not None and self._level_channel != self.channels.level_inputs:
                self._close_di_task()
            if self._di_task is None:
                task = self._daq.create_task()
                self._daq.create_di_lines(task, self.channels.level_inputs)
                self._di_task = task
                self._level_channel = self.channels.level_inputs
            result = self._daq.read_digital_lines(self._di_task, 6)
            if len(result) != 6:
                raise DaqError(f"레벨 입력 개수 오류: {len(result)}")
            return result
        except Exception as exc:  # noqa: BLE001
            self._close_di_task()
            if isinstance(exc, DaqError):
                raise
            raise DaqError(f"레벨 입력 오류: {exc}") from exc

    def write_valves(self, opened: list[bool]) -> list[bool]:
        if len(opened) != 3:
            raise ValueError("밸브 출력은 3개여야 합니다.")
        values = [bool(value) for value in opened]
        if not (self.level_available and self._daq):
            self._last_valves = values
            return values
        try:
            if self._do_task is not None and self._valve_channel != self.channels.valve_outputs:
                self._close_do_task()
            if self._do_task is None:
                task = self._daq.create_task()
                self._daq.create_do_lines(task, self.channels.valve_outputs)
                self._do_task = task
                self._valve_channel = self.channels.valve_outputs
            self._daq.write_digital_lines(self._do_task, values)
            self._last_valves = values
            return values
        except Exception as exc:  # noqa: BLE001
            self._close_do_task()
            raise DaqError(f"밸브 출력 오류: {exc}") from exc

    def read_flame(self) -> bool:
        if not (self.level_available and self._daq):
            return False
        try:
            if self._di_flame_task is not None and self._flame_channel != self.channels.flame_input:
                self._close_di_flame_task()
            if self._di_flame_task is None:
                task = self._daq.create_task()
                self._daq.create_di_lines(task, self.channels.flame_input)
                self._di_flame_task = task
                self._flame_channel = self.channels.flame_input
            values = self._daq.read_digital_lines(self._di_flame_task, 1)
            return bool(values[0]) if values else False
        except Exception as exc:  # noqa: BLE001
            self._close_di_flame_task()
            raise DaqError(f"화염 DI 읽기 오류: {exc}") from exc

    def write_igniter(self, on: bool) -> bool:
        on = bool(on)
        if not (self.level_available and self._daq):
            self._last_igniter = on
            return on
        try:
            if self._do_igniter_task is not None and self._igniter_channel != self.channels.igniter_output:
                self._close_do_igniter_task()
            if self._do_igniter_task is None:
                task = self._daq.create_task()
                self._daq.create_do_lines(task, self.channels.igniter_output)
                self._do_igniter_task = task
                self._igniter_channel = self.channels.igniter_output
            self._daq.write_digital_lines(self._do_igniter_task, [on])
            self._last_igniter = on
            return on
        except Exception as exc:  # noqa: BLE001
            self._close_do_igniter_task()
            raise DaqError(f"점화기 SSR 출력 오류: {exc}") from exc

    def write_inverter_run(self, on: bool) -> bool:
        """Optional FX DO. No-op when inverter_run channel is empty (default)."""
        on = bool(on)
        channel = (self.channels.inverter_run or "").strip()
        if not channel:
            self._last_inverter = on
            return on
        if not (self.level_available and self._daq):
            self._last_inverter = on
            return on
        try:
            if (
                self._do_inverter_task is not None
                and self._inverter_channel != channel
            ):
                self._close_do_inverter_task()
            if self._do_inverter_task is None:
                task = self._daq.create_task()
                self._daq.create_do_lines(task, channel)
                self._do_inverter_task = task
                self._inverter_channel = channel
            self._daq.write_digital_lines(self._do_inverter_task, [on])
            self._last_inverter = on
            return on
        except Exception as exc:  # noqa: BLE001
            self._close_do_inverter_task()
            raise DaqError(f"인버터 RUN 출력 오류: {exc}") from exc

    def write_spare_do4(self, on: bool) -> bool:
        """Spare valve DO4 SINK: True=ON, False=OFF (same wiring as DO0–2)."""
        on = bool(on)
        if not (self.level_available and self._daq):
            self._last_spare_do4 = on
            return on
        try:
            if (
                self._do_spare4_task is not None
                and self._spare_do4_channel != self.channels.spare_do4
            ):
                self._close_do_spare4_task()
            if self._do_spare4_task is None:
                task = self._daq.create_task()
                self._daq.create_do_lines(task, self.channels.spare_do4)
                self._do_spare4_task = task
                self._spare_do4_channel = self.channels.spare_do4
            self._daq.write_digital_lines(self._do_spare4_task, [on])
            self._last_spare_do4 = on
            return on
        except Exception as exc:  # noqa: BLE001
            self._close_do_spare4_task()
            raise DaqError(f"DO 4 출력 오류: {exc}") from exc

    def write_spare_do(self, on: bool) -> bool:
        on = bool(on)
        if not (self.level_available and self._daq):
            self._last_spare_do = on
            return on
        try:
            if (
                self._do_spare_task is not None
                and self._spare_do_channel != self.channels.spare_do
            ):
                self._close_do_spare_task()
            if self._do_spare_task is None:
                task = self._daq.create_task()
                self._daq.create_do_lines(task, self.channels.spare_do)
                self._do_spare_task = task
                self._spare_do_channel = self.channels.spare_do
            self._daq.write_digital_lines(self._do_spare_task, [on])
            self._last_spare_do = on
            return on
        except Exception as exc:  # noqa: BLE001
            self._close_do_spare_task()
            raise DaqError(f"DO 5 출력 오류: {exc}") from exc

    def write_voltage(self, voltage: float, channel: str | None = None) -> float:
        voltage = max(0.0, min(5.0, float(voltage)))
        channel = channel or self.channels.ao_pump
        if not (self.available and self._daq):
            return voltage
        try:
            task = self._ao_tasks.get(channel)
            if task is None:
                task = self._daq.create_task()
                self._daq.create_ao_voltage(task, channel)
                self._ao_tasks[channel] = task
            self._daq.write_ao_voltage(task, voltage)
            self.error = "연결됨"
        except Exception as exc:  # noqa: BLE001
            self.error = f"AO 출력 오류: {exc}"
            self._close_ao_task(channel)
            raise DaqError(self.error) from exc
        return voltage

    def _clear_handle(self, handle: object | None) -> None:
        if handle is None or self._daq is None:
            return
        self._daq.clear_task(handle)  # type: ignore[arg-type]

    def _close_ao_task(self, channel: str | None = None) -> None:
        channels = [channel] if channel is not None else list(self._ao_tasks)
        for task_channel in channels:
            task = self._ao_tasks.pop(task_channel, None)
            self._clear_handle(task)

    def _close_di_task(self) -> None:
        task = self._di_task
        self._di_task = None
        self._level_channel = None
        self._clear_handle(task)

    def _close_di_flame_task(self) -> None:
        task = self._di_flame_task
        self._di_flame_task = None
        self._flame_channel = None
        self._clear_handle(task)

    def _close_do_task(self) -> None:
        task = self._do_task
        self._do_task = None
        self._valve_channel = None
        self._clear_handle(task)

    def _close_do_igniter_task(self) -> None:
        task = self._do_igniter_task
        self._do_igniter_task = None
        self._igniter_channel = None
        self._clear_handle(task)

    def _close_do_inverter_task(self) -> None:
        task = self._do_inverter_task
        self._do_inverter_task = None
        self._inverter_channel = None
        self._clear_handle(task)

    def _close_do_spare4_task(self) -> None:
        task = self._do_spare4_task
        self._do_spare4_task = None
        self._spare_do4_channel = None
        self._clear_handle(task)

    def _close_do_spare_task(self) -> None:
        task = self._do_spare_task
        self._do_spare_task = None
        self._spare_do_channel = None
        self._clear_handle(task)

    def _close_tc_task_unlocked(self) -> None:
        task = self._tc_task
        self._tc_task = None
        self._tc_channels = None
        self._clear_handle(task)

    def _close_tc_task(self) -> None:
        with self._tc_lock:
            self._close_tc_task_unlocked()

    def close_thermocouples(self) -> None:
        self._close_tc_task()

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
            self.write_spare_do4(False)
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
        self._close_do_spare4_task()
        self._close_do_spare_task()
        self._close_tc_task()
        self._close_ao_task()
