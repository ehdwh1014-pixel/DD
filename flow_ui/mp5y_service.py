"""Autonics MP5Y-25 Modbus RTU reader (USB-RS485).

Default link settings match Autonics factory defaults:
  COM3, 9600 baud, 8N2, slave address 1

PV is read from input registers:
  301002 / 0x03E9  : measurement value (32-bit across 0x03E9 + 0x03EA)
  301004 / 0x03EB  : decimal point (DOT)
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from flow_ui.flow_math import DEFAULT_PULSE_ML, frequency_to_ccpm


class Mp5yError(RuntimeError):
    """Raised when MP5Y communication fails while hardware mode is expected."""


@dataclass
class Mp5yConfig:
    port: str = "COM3"
    slave_id: int = 1
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 2
    bytesize: int = 8
    timeout_s: float = 0.5
    # "frequency_hz": MP5Y shows Hz, convert with pulse_ml
    # "flow_ccpm": MP5Y already shows cc/min (prescale applied on meter)
    value_mode: str = "frequency_hz"
    pulse_ml: float = DEFAULT_PULSE_ML
    pv_address: int = 0x03E9


class Mp5yService:
    """Read scaled PV from MP5Y-25 over Modbus RTU."""

    def __init__(self, config: Mp5yConfig | None = None) -> None:
        self.config = config or Mp5yConfig()
        self.available = False
        self.error = "MP5Y 통신 확인 중"
        self.device_summary = "MP5Y-25 대기"
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
            self.device_summary = (
                f"MP5Y-25 {self.config.port} addr={self.config.slave_id} "
                f"{self.config.baudrate} 8{self.config.parity}{self.config.stopbits}"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.error = f"MP5Y 통신 오류: {exc}"
            self.device_summary = f"MP5Y-25 {self.config.port} 미연결"
            self._close_client()
            return False

    def read_flow(self, pulse_ml: float | None = None) -> tuple[float, float, int, int]:
        """Return (flow_ccpm, frequency_hz_or_nan, raw_int, dot).

        Always attempts a live Modbus read. Callers that want offline UI
        behavior should catch Mp5yError and use simulate_flow().
        """
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

        client = ModbusSerialClient(
            port=self.config.port,
            baudrate=self.config.baudrate,
            parity=self.config.parity,
            stopbits=self.config.stopbits,
            bytesize=self.config.bytesize,
            timeout=self.config.timeout_s,
        )
        if not client.connect():
            raise Mp5yError(f"{self.config.port} 연결 실패")
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

    def _read_pv_once(self) -> tuple[float, float, int, int]:
        self._ensure_client()
        assert self._client is not None
        result = self._client.read_input_registers(
            address=self.config.pv_address,
            count=3,
            device_id=self.config.slave_id,
        )
        if result is None or result.isError():
            raise Mp5yError(f"Modbus 응답 오류: {result}")

        regs = list(result.registers)
        if len(regs) < 3:
            raise Mp5yError(f"레지스터 부족: {regs}")

        raw = self._decode_s32(regs[0], regs[1])
        dot = int(regs[2]) & 0xFF
        if dot < 0 or dot > 4:
            dot = 0
        scaled = raw / (10 ** dot)

        if self.config.value_mode == "flow_ccpm":
            flow = float(scaled)
            hz = float("nan") if self.config.pulse_ml <= 0 else flow / (self.config.pulse_ml * 60.0)
        else:
            hz = float(scaled)
            flow = frequency_to_ccpm(hz, self.config.pulse_ml)
        return flow, hz, raw, dot

    @staticmethod
    def _decode_s32(high: int, low: int) -> int:
        value = ((high & 0xFFFF) << 16) | (low & 0xFFFF)
        if value & 0x80000000:
            value -= 0x100000000
        return value

    def _simulate(self) -> tuple[float, float, int, int]:
        elapsed = time.monotonic() - self._sim_started
        hz = 4.35 + 0.25 * math.sin(elapsed * 0.7) + random.uniform(-0.05, 0.05)
        flow = frequency_to_ccpm(hz, self.config.pulse_ml)
        raw = int(round(hz * 100))  # pretend DOT=2
        self._last_flow, self._last_hz, self._last_raw, self._last_dot = flow, hz, raw, 2
        self.error = "시뮬레이션 모드"
        self.device_summary = f"MP5Y-25 시뮬레이션 ({self.config.port})"
        return flow, hz, raw, 2
