"""Unit tests for NI-DAQmx lite constant values."""

from __future__ import annotations

import unittest

from flow_ui.nidaqmx_lite import (
    DAQmx_Val_ChanPerLine,
    DAQmx_Val_GroupByChannel,
)


class TestNidaqmxLiteConstants(unittest.TestCase):
    def test_digital_grouping_constants(self) -> None:
        # Wrong zeros here broke DI/DO in the slim EXE (level valves stopped working).
        self.assertEqual(DAQmx_Val_ChanPerLine, 1030)
        self.assertEqual(DAQmx_Val_GroupByChannel, 1041)

    def test_expand_channel_lines(self) -> None:
        from flow_ui.nidaqmx_lite import expand_channel_lines

        self.assertEqual(
            expand_channel_lines("cDAQ2Mod2/port0/line0:5"),
            [f"cDAQ2Mod2/port0/line{i}" for i in range(6)],
        )
        self.assertEqual(
            expand_channel_lines("cDAQ2Mod3/port0/line0:2"),
            [
                "cDAQ2Mod3/port0/line0",
                "cDAQ2Mod3/port0/line1",
                "cDAQ2Mod3/port0/line2",
            ],
        )


if __name__ == "__main__":
    unittest.main()
