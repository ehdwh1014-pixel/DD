"""Unit tests for NI device auto-mapping helpers."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from flow_ui.daq_service import (
    ChannelConfig,
    apply_module_names,
    device_from_channel,
    discover_modules_by_product,
    product_matches,
)


class TestDaqDeviceHelpers(unittest.TestCase):
    def test_device_from_channel(self) -> None:
        self.assertEqual(device_from_channel("cDAQ1Mod1/ao0"), "cDAQ1Mod1")

    def test_product_matches(self) -> None:
        self.assertTrue(product_matches("NI 9264 (Sim)", "9264"))
        self.assertTrue(product_matches("NI9422", "9422"))
        self.assertFalse(product_matches("NI 9205", "9264"))

    def test_discover_modules_by_product(self) -> None:
        devices = {
            "cDAQ1Mod1": SimpleNamespace(
                name="cDAQ1Mod1", product_type="NI 9264 (Sim)"
            ),
            "cDAQ1Mod2": SimpleNamespace(
                name="cDAQ1Mod2", product_type="NI 9422 (Sim)"
            ),
            "cDAQ1Mod3": SimpleNamespace(
                name="cDAQ1Mod3", product_type="NI 9477 (Sim)"
            ),
            "cDAQ1Mod4": SimpleNamespace(
                name="cDAQ1Mod4", product_type="NI 9214 (Sim)"
            ),
        }
        found = discover_modules_by_product(devices)
        self.assertEqual(
            found,
            {
                "ao": "cDAQ1Mod1",
                "di": "cDAQ1Mod2",
                "do": "cDAQ1Mod3",
                "tc": "cDAQ1Mod4",
            },
        )

    def test_apply_module_names(self) -> None:
        channels = ChannelConfig()
        apply_module_names(
            channels,
            ao="cDAQ1Mod1",
            di="cDAQ1Mod2",
            do="cDAQ1Mod3",
            tc="cDAQ1Mod4",
        )
        self.assertEqual(channels.ao_pump, "cDAQ1Mod1/ao0")
        self.assertEqual(channels.level_inputs, "cDAQ1Mod2/port0/line0:5")
        self.assertEqual(channels.valve_outputs, "cDAQ1Mod3/port0/line0:2")
        self.assertEqual(channels.spare_do4, "cDAQ1Mod3/port0/line4")
        self.assertFalse(hasattr(ChannelConfig(), "flame_input"))
        self.assertFalse(hasattr(ChannelConfig(), "spare_do"))
        self.assertEqual(channels.inverter_run, "")
        self.assertEqual(channels.tc_k_inputs, "cDAQ1Mod4/ai0:4")


if __name__ == "__main__":
    unittest.main()
