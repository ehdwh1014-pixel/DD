"""Simulation-mode checks for DAQ fallback, level valves, I/O TEST, and PI loop."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
try:
    import tkinter  # noqa: F401

    _TK_OK = True
except Exception:  # noqa: BLE001
    _TK_OK = False


class TestFakeDaqLevelValves(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("PULSEFLOW_SIM")
        os.environ["PULSEFLOW_SIM"] = "1"
        from flow_ui.daq_service import DaqService

        self.svc = DaqService()
        self.assertTrue(self.svc.simulated)
        self.assertTrue(self.svc.check_connection())
        self.fake = self.svc._daq

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("PULSEFLOW_SIM", None)
        else:
            os.environ["PULSEFLOW_SIM"] = self._prev

    def test_supply_opens_on_low_and_closes_on_high(self) -> None:
        from flow_ui.level_control import ValveRole, decide_valve

        self.fake.set_di("cDAQ2Mod2/port0/line1", True)  # tank1 LOW
        levels = self.svc.read_levels()
        opened = decide_valve(ValveRole.SUPPLY, levels[0], levels[1], False).opened
        self.assertTrue(opened)
        self.svc.write_valves([opened, False, False])
        self.assertEqual(self.fake.valve_bits(), [True, False, False])

        self.fake.set_di("cDAQ2Mod2/port0/line1", False)
        self.fake.set_di("cDAQ2Mod2/port0/line0", True)  # tank1 HIGH
        levels = self.svc.read_levels()
        opened = decide_valve(ValveRole.SUPPLY, levels[0], levels[1], True).opened
        self.assertFalse(opened)
        self.svc.write_valves([opened, False, False])
        self.assertEqual(self.fake.valve_bits()[0], False)

    def test_drain_opens_on_high(self) -> None:
        from flow_ui.level_control import ValveRole, decide_valve

        self.fake.set_di("cDAQ2Mod2/port0/line2", True)  # tank2 HIGH
        levels = self.svc.read_levels()
        opened = decide_valve(ValveRole.DRAIN, levels[2], levels[3], False).opened
        self.assertTrue(opened)
        self.svc.write_valves([False, opened, False])
        self.assertTrue(self.fake.valve_bits()[1])

    def test_simulated_mp5y_and_pi_raise_ao(self) -> None:
        from flow_ui.control import LowPassFilter, PIController
        from flow_ui.mp5y_service import Mp5yService

        meter = Mp5yService()
        self.assertTrue(meter.check_connection())
        flow, _hz, _raw, _dot = meter.read_flow()
        self.assertGreater(flow, 50.0)
        pi = PIController(0.0, 5.0)
        lpf = LowPassFilter()
        pv = lpf.update(flow, 0.12, 0.8)
        ao = pi.update(200.0, pv, 0.02, 0.005, 0.12)
        self.assertGreater(ao, 0.0)
        self.svc.write_voltage(ao)
        self.assertAlmostEqual(self.fake.ao[self.svc.channels.ao_pump], ao)

    def test_io_test_style_pause_does_not_auto_write(self) -> None:
        from flow_ui.level_control import ValveRole, decide_valve

        io_test_active = True
        self.fake.set_di("cDAQ2Mod2/port0/line1", True)
        if io_test_active:
            commands = [False, False, False]
        else:
            levels = self.svc.read_levels()
            commands = [
                decide_valve(ValveRole.SUPPLY, levels[0], levels[1], False).opened,
                False,
                False,
            ]
        self.svc.write_valves(commands)
        self.assertEqual(self.fake.valve_bits(), [False, False, False])


@unittest.skipUnless(_TK_OK, "python3-tk is not installed")
class TestAppSimulation(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("PULSEFLOW_SIM")
        os.environ["PULSEFLOW_SIM"] = "1"
        self._tmpdir = tempfile.TemporaryDirectory()
        runtime = Path(self._tmpdir.name)
        import flow_ui.app as app_mod

        self.info = patch.object(app_mod.messagebox, "showinfo")
        self.err = patch.object(app_mod.messagebox, "showerror")
        self.info.start()
        self.err.start()
        with patch.object(app_mod.FlowControlApp, "_runtime_dir", return_value=runtime):
            self.app = app_mod.FlowControlApp()
        self.app.withdraw()
        self.app.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.app.on_close()
        except Exception:  # noqa: BLE001
            pass
        self.info.stop()
        self.err.stop()
        self._tmpdir.cleanup()
        if self._prev is None:
            os.environ.pop("PULSEFLOW_SIM", None)
        else:
            os.environ["PULSEFLOW_SIM"] = self._prev

    def test_level_auto_starts_and_stops_overflow_on_high(self) -> None:
        app = self.app
        self.assertTrue(app.daq.simulated)
        self.assertTrue(app.level_running)
        fake = app.daq._daq
        fake.set_di("cDAQ2Mod2/port0/line1", True)  # LOW -> fill
        app._update_levels()
        self.assertTrue(app.valve_commands[0], "급수 밸브가 LOW에서 열려야 함")
        fake.set_di("cDAQ2Mod2/port0/line0", True)  # HIGH -> stop fill
        fake.set_di("cDAQ2Mod2/port0/line1", False)
        app._update_levels()
        self.assertFalse(app.valve_commands[0], "급수 밸브가 HIGH에서 닫혀야 함")

    def test_io_test_pauses_auto_and_manual_do0_works(self) -> None:
        app = self.app
        fake = app.daq._daq
        app.open_ao_popup()
        self.assertTrue(app.io_test_active)
        fake.set_di("cDAQ2Mod2/port0/line1", True)
        app._update_levels()
        self.assertEqual(app.valve_commands, [False, False, False])
        app.set_test_valve(0, True)
        self.assertTrue(app.valve_commands[0])
        self.assertTrue(fake.valve_bits()[0])
        app._close_ao_popup()
        self.assertFalse(app.io_test_active)
        # Auto resumes and LOW still on -> supply should open again.
        app._update_levels()
        self.assertTrue(app.valve_commands[0])

    def test_feedback_moves_ao_when_sv_above_pv(self) -> None:
        app = self.app
        app.sv.set("200")
        if hasattr(app, "sv_input"):
            app.sv_input.set("200")
        app.toggle_feedback()
        self.assertTrue(app.feedback_running)
        app._update_feedback()
        self.assertGreater(app.current_ao, 0.0)

    def test_valve_name_save_roundtrip(self) -> None:
        app = self.app
        app.valve_name_vars[0].set("반응기 급수")
        app.save_valve_names()
        data = app.SETTINGS_PATH.read_text(encoding="utf-8")
        self.assertIn("반응기 급수", data)


if __name__ == "__main__":
    unittest.main()
