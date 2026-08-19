"""Unit tests for flow conversion, PI / LPF, and MP5Y decoding helpers."""

from __future__ import annotations

import unittest

from flow_ui.control import LowPassFilter, PIController
from flow_ui.flow_math import frequency_to_ccpm, pulses_to_ccpm
from flow_ui.level_control import SensorType, ValveRole, decide_valve
from flow_ui.mp5y_service import Mp5yConfig, Mp5yService


class FlowMathTests(unittest.TestCase):
    def test_120_ccpm_from_frequency(self) -> None:
        # 120 / (0.46 * 60) ≈ 4.3478 Hz
        flow = frequency_to_ccpm(120.0 / (0.46 * 60.0), 0.46)
        self.assertAlmostEqual(flow, 120.0, places=5)

    def test_pulses_over_window(self) -> None:
        # 261 pulses / 60 s * 0.46 * 60 ≈ 120.06
        flow = pulses_to_ccpm(261, 60.0, 0.46)
        self.assertAlmostEqual(flow, 120.06, places=2)


class ControlTests(unittest.TestCase):
    def test_lpf_passthrough_when_disabled(self) -> None:
        lpf = LowPassFilter()
        self.assertEqual(lpf.update(10.0, 0.1, 0.0), 10.0)
        self.assertEqual(lpf.update(20.0, 0.1, 0.0), 20.0)

    def test_pi_moves_toward_setpoint(self) -> None:
        pi = PIController(0.0, 5.0)
        out1 = pi.update(setpoint=120, process_value=100, p_gain=0.02, i_gain=0.0, dt=0.1)
        out2 = pi.update(setpoint=120, process_value=110, p_gain=0.02, i_gain=0.0, dt=0.1)
        self.assertGreater(out1, out2)
        self.assertGreater(out1, 0.0)

    def test_pi_clamps(self) -> None:
        pi = PIController(0.0, 5.0)
        out = pi.update(setpoint=1000, process_value=0, p_gain=1.0, i_gain=0.0, dt=0.1)
        self.assertEqual(out, 5.0)


class Mp5yDecodeTests(unittest.TestCase):
    def test_decode_s32(self) -> None:
        self.assertEqual(Mp5yService._decode_s32(0, 1200), 1200)
        self.assertEqual(Mp5yService._decode_s32(0xFFFF, 0xFFFF), -1)

    def test_decode_rejects_blown_int32(self) -> None:
        service = Mp5yService(Mp5yConfig(pv_format="int32"))
        # Former bug: merging a non-PV word created ~75e6.
        self.assertEqual(service._decode_pv(0x0486, 0x0F00), service._decode_s16(0x0486))

    def test_simulate_returns_positive_flow(self) -> None:
        service = Mp5yService(Mp5yConfig(port="COM9", value_mode="flow_ccpm"))
        flow, hz, raw, dot = service.simulate_flow()
        self.assertGreater(flow, 0.0)
        self.assertGreater(hz, 0.0)
        self.assertIsInstance(raw, int)
        self.assertEqual(dot, 2)


class LevelControlTests(unittest.TestCase):
    def test_supply_low_opens_and_high_closes(self) -> None:
        self.assertTrue(decide_valve(ValveRole.SUPPLY, False, True, False).opened)
        self.assertFalse(decide_valve(ValveRole.SUPPLY, True, False, True).opened)

    def test_drain_low_closes_and_high_opens(self) -> None:
        self.assertFalse(decide_valve(ValveRole.DRAIN, False, True, True).opened)
        self.assertTrue(decide_valve(ValveRole.DRAIN, True, False, False).opened)

    def test_neither_holds_and_both_closes(self) -> None:
        self.assertTrue(decide_valve(ValveRole.SUPPLY, False, False, True).opened)
        collision = decide_valve(ValveRole.DRAIN, True, True, True)
        self.assertFalse(collision.opened)
        self.assertTrue(collision.fault)

    def test_four_point_supply_both_on_closes(self) -> None:
        # 4접점: H ON + L ON = 고수위 → 급수 닫힘 (충돌 아님)
        d = decide_valve(ValveRole.SUPPLY, True, True, True, SensorType.FOUR_POINT)
        self.assertFalse(d.opened)
        self.assertFalse(d.fault)

    def test_four_point_supply_only_low_opens(self) -> None:
        # 4접점: H OFF + L ON = 저수위 → 급수 열림
        d = decide_valve(ValveRole.SUPPLY, False, True, False, SensorType.FOUR_POINT)
        self.assertTrue(d.opened)

    def test_four_point_supply_neither_opens(self) -> None:
        # 4접점: 둘 다 OFF = 매우 저수위 → 급수 열림
        d = decide_valve(ValveRole.SUPPLY, False, False, False, SensorType.FOUR_POINT)
        self.assertTrue(d.opened)


if __name__ == "__main__":
    unittest.main()
