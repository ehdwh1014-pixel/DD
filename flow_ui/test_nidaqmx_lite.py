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


if __name__ == "__main__":
    unittest.main()
