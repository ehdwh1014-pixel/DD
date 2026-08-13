"""Autonics MP5Y-25 Modbus RTU reader (USB-RS485).

Default link settings match Autonics factory defaults:
  COM9, 9600 baud, 8N2, slave address 1

Input registers (Func 04):
  0x03E9 / 0x03EA : PV (Autonics 2-word value, -19999..99999)
  0x03EB          : DOT decimal point
  0x03ED          : MODE (0=F1 frequency ... 15=F16)
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from flow_ui.flow_math import DEFAULT_PULSE_ML, frequency_to_ccpm


class Mp5yError(RuntimeError):
    """Raised when MP5Y communication fails while hardware mode is expected."""


MODE_NAMES = {
    0: "F1 주파수",
    1: "F2 통과속도",
    2: "F3 주기",
    3: "F4 통과시간",
    4: "F5 시간간격",
    5: "F6 시간차",
    6: "F7 절대비",
    7: "F8 오차비",
    8: "F9 농도",
    9: "F10 오차",
    10: "F11 측장1",
    11: "F12 간격",
    12: "F13 적산",
    13: "F14 가감산",
    14: "F15 가감산(위상)",
    15: "F16 측장2",
}


@dataclass
class Mp5yConfig:
    port: str = "COM9"
    slave_id: int = 1
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 2
    bytesize: int = 8
    timeout_s: float = 1.0
    # "frequency_hz": MP5Y shows Hz, convert with pulse_ml * 60
    # "flow_ccpm": MP5Y already shows cc/min (prescale 27.6 = 0.46*60)
    value_mode: str = "flow_ccpm"
    pulse_ml: float = DEFAULT_PULSE_ML
    pv_address: int = 0x03E9
    # int16: use only first PV register (safe default)
    # int32: binary high/low word
    # dec32: Autonics decimal high*10000 + low
    pv_format: str = "int16"
    # None/-1: use DOT register from MP5Y. 0..4: force decimal places.
    decimal_places: int | None = None


class Mp5yService:
    """Read scaled PV from MP5Y-25 over Modbus RTU."""

    def __init__(self, config: Mp5yConfig | None = None) -> None:
        self.config = config or Mp5yConfig()
        self.available = False
        self.error = "MP5Y 통신 확인 중"
        self.device_summary = "MP5Y-25 대기"
        self.last_mode = -1
        self.last_regs: tuple[int, int, int] = (0, 0, 0)
        self._client = None
        self._last_flow = 0.0
        self._last_hz = 0.0
        self._last_raw = 0
        self._last_dot = 0
        self._sim_started = time.monotonic()

    def check_connection(self) -> bool:
        try:
            self._ensure_client()
            flow, hz, raw, dot = self._read_pv_once()
            self._last_flow, self._last_hz = flow, hz
            self._last_raw, self._last_dot = raw, dot
            self.available = True
            self.error = "연결됨"
            mode_name = MODE_NAMES.get(self.last_mode, f"mode={self.last_mode}")
            self.device_summary = (
                f"MP5Y-25 {self.config.port} addr={self.config.slave_id} "
                f"{self.config.baudrate} 8{self.config.parity}{self.config.stopbits} · {mode_name}"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.error = f"MP5Y 통신 오류: {exc}"
            self.device_summary = f"MP5Y-25 {self.config.port} 미연결"
            self._close_client()
            return False

    def port_present(self) -> bool:
        """Return whether the configured serial port is enumerated by Windows."""
        try:
            from serial.tools import list_ports

            expected = self.config.port.strip().casefold()
            return any(
                str(port.device).strip().casefold() == expected
                for port in list_ports.comports()
            )
        except Exception:  # noqa: BLE001 - status aid only
            return False

    def read_flow(self, pulse_ml: float | None = None) -> tuple[float, float, int, int]:
        """Return (flow_ccpm, frequency_hz_or_nan, raw_int, dot)."""
        if pulse_ml is not None:
            self.config.pulse_ml = pulse_ml

        try:
            flow, hz, raw, dot = self._read_pv_once()
            self._last_flow, self._last_hz = flow, hz
            self._last_raw, self._last_dot = raw, dot
            self.available = True
            self.error = "연결됨"
            return flow, hz, raw, dot
        except Exception as exc:  # noqa: BLE001
            self.error = f"MP5Y 읽기 오류: {exc}"
            self.available = False
            self._close_client()
            raise Mp5yError(self.error) from exc

    def simulate_flow(self, pulse_ml: float | None = None) -> tuple[float, float, int, int]:
        if pulse_ml is not None:
            self.config.pulse_ml = pulse_ml
        return self._simulate()

    def close(self) -> None:
        self._close_client()

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError as exc:
            raise Mp5yError("pymodbus / pyserial 패키지가 필요합니다.") from exc

        base: dict[str, object] = {
            "port": self.config.port,
            "baudrate": self.config.baudrate,
            "parity": self.config.parity,
            "stopbits": self.config.stopbits,
            "bytesize": self.config.bytesize,
            "timeout": self.config.timeout_s,
        }
        client = None
        last_exc: Exception | None = None
        # Try modern FramerType, then legacy method='rtu', then bare kwargs.
        attempts: list[dict[str, object]] = [dict(base)]
        try:
            from pymodbus import FramerType  # type: ignore

            modern = dict(base)
            modern["framer"] = FramerType.RTU
            attempts.insert(0, modern)
        except Exception:  # noqa: BLE001
            legacy = dict(base)
            legacy["method"] = "rtu"
            attempts.insert(0, legacy)

        for kwargs in attempts:
            try:
                candidate = ModbusSerialClient(**kwargs)
                if not candidate.connect():
                    try:
                        candidate.close()
                    except Exception:  # noqa: BLE001
                        pass
                    last_exc = Mp5yError(f"{self.config.port} 연결 실패")
                    continue
                client = candidate
                break
            except TypeError as exc:
                last_exc = exc
                continue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue

        if client is None:
            raise Mp5yError(str(last_exc) if last_exc else f"{self.config.port} 연결 실패")
        # USB-RS485 adapters often need a short settle before the first RTU frame.
        time.sleep(0.08)
        self._client = client

    def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    def _read_input_registers(self, address: int, count: int):
        assert self._client is not None
        # pymodbus renamed slave= → device_id= across 3.7/3.8.
        try:
            return self._client.read_input_registers(
                address=address,
                count=count,
                device_id=self.config.slave_id,
            )
        except TypeError:
            try:
                return self._client.read_input_registers(
                    address=address,
                    count=count,
                    slave=self.config.slave_id,
                )
            except TypeError:
                return self._client.read_input_registers(
                    address,
                    count,
                    self.config.slave_id,
                )

    def _read_pv_once(self) -> tuple[float, float, int, int]:
        self._ensure_client()
        assert self._client is not None
        # Prefer PV+DOT+UNIT+MODE (5 regs). Fall back to PV+DOT if meter rejects.
        last_error: Exception | None = None
        regs: list[int] | None = None
        for count in (5, 3):
            try:
                result = self._read_input_registers(self.config.pv_address, count)
                if result is None or result.isError():
                    last_error = Mp5yError(f"Modbus 응답 오류: {result}")
                    continue
                regs = list(result.registers)
                if len(regs) < 3:
                    last_error = Mp5yError(f"레지스터 부족: {regs}")
                    regs = None
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                regs = None
                # Re-open port once; some adapters drop the first frame after connect.
                self._close_client()
                self._ensure_client()
        if regs is None:
            raise Mp5yError(str(last_error) if last_error else "Modbus 응답 없음")

        word0, word1, dot_reg = regs[0], regs[1], regs[2]
        mode_reg = regs[4] if len(regs) >= 5 else 0
        self.last_regs = (word0, word1, dot_reg)
        self.last_mode = int(mode_reg) & 0xFF

        raw = self._decode_pv(word0, word1)
        dot = int(dot_reg) & 0xFF
        if self.config.decimal_places is not None and self.config.decimal_places >= 0:
            dot = int(self.config.decimal_places)
        if dot < 0 or dot > 4:
            dot = 0
        # This scaled value should match the MP5Y front display.
        scaled = raw / (10 ** dot)

        if self.config.value_mode == "flow_ccpm":
            flow = float(scaled)
            hz = float("nan") if self.config.pulse_ml <= 0 else flow / (self.config.pulse_ml * 60.0)
        else:
            hz = float(scaled)
            # cc/min = Hz × (ml/P) × 60
            flow = frequency_to_ccpm(hz, self.config.pulse_ml)
        return flow, hz, raw, dot

    def _decode_pv(self, word0: int, word1: int) -> int:
        fmt = self.config.pv_format
        if fmt == "int32":
            raw = self._decode_s32(word0, word1)
        elif fmt == "dec32":
            raw = self._decode_dec32(word0, word1)
        else:
            raw = self._decode_s16(word0)

        # Guard against the previous blow-up where a non-PV word was merged.
        if abs(raw) > 99999:
            raw = self._decode_s16(word0)
        return raw

    @staticmethod
    def _decode_s16(word: int) -> int:
        value = word & 0xFFFF
        if value & 0x8000:
            value -= 0x10000
        return value

    @staticmethod
    def _decode_s32(high: int, low: int) -> int:
        value = ((high & 0xFFFF) << 16) | (low & 0xFFFF)
        if value & 0x80000000:
            value -= 0x100000000
        return value

    @staticmethod
    def _decode_dec32(high: int, low: int) -> int:
        """Autonics-style split decimal long used by several panel meters."""
        high_s = Mp5yService._decode_s16(high)
        low_u = low & 0xFFFF
        if low_u > 9999:
            # Not a decimal split; fall back.
            return Mp5yService._decode_s32(high, low)
        sign = -1 if high_s < 0 else 1
        return sign * (abs(high_s) * 10000 + low_u)

    def _simulate(self) -> tuple[float, float, int, int]:
        elapsed = time.monotonic() - self._sim_started
        hz = 4.35 + 0.25 * math.sin(elapsed * 0.7) + random.uniform(-0.05, 0.05)
        flow = frequency_to_ccpm(hz, self.config.pulse_ml)
        raw = int(round(hz * 100))  # pretend DOT=2
        self.last_mode = 0
        self.last_regs = (raw, 0, 2)
        self._last_flow, self._last_hz, self._last_raw, self._last_dot = flow, hz, raw, 2
        self.error = "시뮬레이션 모드"
        self.device_summary = f"MP5Y-25 시뮬레이션 ({self.config.port}) · F1 주파수"
        return flow, hz, raw, 2
