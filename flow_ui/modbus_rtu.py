"""Minimal Modbus RTU client (function 0x04 input registers) over pyserial.

Replaces the heavy pymodbus dependency for MP5Y panel-meter reads.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serial import Serial


class ModbusRtuError(RuntimeError):
    """Raised when a Modbus RTU frame cannot be completed."""


def crc16_modbus(data: bytes) -> int:
    """Modbus RTU CRC-16 (poly 0xA001)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def build_read_input_registers(slave: int, address: int, count: int) -> bytes:
    if not 1 <= slave <= 247:
        raise ModbusRtuError(f"잘못된 슬레이브 주소: {slave}")
    if not 1 <= count <= 125:
        raise ModbusRtuError(f"잘못된 레지스터 개수: {count}")
    payload = struct.pack(">BBHH", slave & 0xFF, 0x04, address & 0xFFFF, count & 0xFFFF)
    crc = crc16_modbus(payload)
    return payload + struct.pack("<H", crc)


def parse_read_input_registers(response: bytes, slave: int, count: int) -> list[int]:
    if len(response) < 5:
        raise ModbusRtuError(f"응답이 너무 짧음: {response!r}")
    if response[0] != (slave & 0xFF):
        raise ModbusRtuError(f"슬레이브 불일치: {response[0]} != {slave}")
    function = response[1]
    if function & 0x80:
        code = response[2] if len(response) > 2 else -1
        raise ModbusRtuError(f"Modbus 예외 코드: {code}")
    if function != 0x04:
        raise ModbusRtuError(f"예상치 못한 기능코드: {function}")
    byte_count = response[2]
    expected = 3 + byte_count + 2
    if len(response) < expected:
        raise ModbusRtuError(f"응답 길이 부족: {len(response)} < {expected}")
    if byte_count != count * 2:
        raise ModbusRtuError(f"바이트 수 불일치: {byte_count} != {count * 2}")
    body = response[: 3 + byte_count]
    got_crc = response[3 + byte_count] | (response[3 + byte_count + 1] << 8)
    if crc16_modbus(body) != got_crc:
        raise ModbusRtuError("CRC 오류")
    registers: list[int] = []
    data = response[3 : 3 + byte_count]
    for index in range(0, byte_count, 2):
        registers.append((data[index] << 8) | data[index + 1])
    return registers


class ModbusRtuClient:
    """Tiny serial Modbus RTU client kept open between polls."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: int = 2,
        bytesize: int = 8,
        timeout_s: float = 1.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self.timeout_s = timeout_s
        self._serial: Serial | None = None

    def connect(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        try:
            import serial
        except ImportError as exc:
            raise ModbusRtuError("pyserial 패키지가 필요합니다.") from exc
        parity_map = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
        }
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=parity_map.get(self.parity.upper(), serial.PARITY_NONE),
            stopbits=self.stopbits,
            timeout=self.timeout_s,
            write_timeout=self.timeout_s,
        )
        # USB-RS485 adapters often need a short settle before the first frame.
        import time

        time.sleep(0.08)

    def close(self) -> None:
        serial_port = self._serial
        self._serial = None
        if serial_port is None:
            return
        try:
            serial_port.close()
        except Exception:  # noqa: BLE001
            pass

    def read_input_registers(self, slave: int, address: int, count: int) -> list[int]:
        self.connect()
        assert self._serial is not None
        request = build_read_input_registers(slave, address, count)
        try:
            self._serial.reset_input_buffer()
        except Exception:  # noqa: BLE001
            pass
        self._serial.write(request)
        self._serial.flush()
        # Response: addr + func + byte_count + data + CRC
        header = self._read_exact(3)
        if header[1] & 0x80:
            # Exception: addr + (func|0x80) + exception + CRC
            rest = self._read_exact(3)
            parse_read_input_registers(header + rest, slave, count)
            raise ModbusRtuError("Modbus 예외")
        byte_count = header[2]
        rest = self._read_exact(byte_count + 2)
        return parse_read_input_registers(header + rest, slave, count)

    def _read_exact(self, size: int) -> bytes:
        assert self._serial is not None
        data = self._serial.read(size)
        if len(data) != size:
            raise ModbusRtuError(
                f"응답 타임아웃 ({len(data)}/{size} bytes on {self.port})"
            )
        return data
