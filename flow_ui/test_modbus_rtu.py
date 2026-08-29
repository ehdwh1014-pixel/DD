"""Unit tests for minimal Modbus RTU framing."""

from __future__ import annotations

import unittest

from flow_ui.modbus_rtu import (
    ModbusRtuError,
    build_read_input_registers,
    crc16_modbus,
    parse_read_input_registers,
)


class TestModbusRtu(unittest.TestCase):
    def test_crc_known_vector(self) -> None:
        # 01 04 03 E9 00 01 → CRC low/high = E0 7A
        payload = bytes([0x01, 0x04, 0x03, 0xE9, 0x00, 0x01])
        self.assertEqual(crc16_modbus(payload), 0x7AE0)
        frame = build_read_input_registers(1, 0x03E9, 1)
        self.assertEqual(frame, payload + bytes([0xE0, 0x7A]))

    def test_build_and_parse_roundtrip(self) -> None:
        request = build_read_input_registers(1, 0x03E9, 3)
        self.assertEqual(request[:6], bytes([0x01, 0x04, 0x03, 0xE9, 0x00, 0x03]))
        # Fake response: addr, func, byte_count, 3 regs, crc
        body = bytes([0x01, 0x04, 0x06, 0x04, 0xB0, 0x00, 0x00, 0x00, 0x02])
        crc = crc16_modbus(body)
        response = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        regs = parse_read_input_registers(response, 1, 3)
        self.assertEqual(regs, [0x04B0, 0x0000, 0x0002])

    def test_crc_error(self) -> None:
        body = bytes([0x01, 0x04, 0x02, 0x00, 0x01, 0x00, 0x00])
        with self.assertRaises(ModbusRtuError):
            parse_read_input_registers(body, 1, 1)


if __name__ == "__main__":
    unittest.main()
