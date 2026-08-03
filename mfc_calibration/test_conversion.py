"""MFC 환산식 단위 테스트."""

from __future__ import annotations

import unittest

from mfc_calibration.conversion import (
    CALIBRATION_POINTS,
    FLOW_INTERCEPT,
    FLOW_SLOPE,
    PREVIOUS_INTERCEPT,
    PREVIOUS_SLOPE,
    compensated_setpoint,
    corrected_from_measured,
    expected_measured,
)


class ConversionTests(unittest.TestCase):
    def test_formula_matches_graph(self) -> None:
        self.assertEqual(FLOW_SLOPE, 1.15)
        self.assertEqual(FLOW_INTERCEPT, 3.3333)

    def test_previous_formula_documented(self) -> None:
        self.assertEqual(PREVIOUS_SLOPE, 0.9337)
        self.assertEqual(PREVIOUS_INTERCEPT, 4.6452)

    def test_expected_near_calibration_points(self) -> None:
        for setpoint, measured in CALIBRATION_POINTS:
            predicted = expected_measured(setpoint)
            self.assertAlmostEqual(predicted, measured, delta=0.4)

    def test_round_trip_compensate_and_expected(self) -> None:
        for desired in (5.0, 10.0, 30.0, 40.0, 50.0, 60.0):
            command = compensated_setpoint(desired)
            self.assertAlmostEqual(expected_measured(command), desired, places=6)

    def test_correct_inverse_of_expected(self) -> None:
        for setpoint in (5.0, 10.0, 30.0, 40.0, 50.0):
            measured = expected_measured(setpoint)
            self.assertAlmostEqual(corrected_from_measured(measured), setpoint, places=6)

    def test_specific_graph_predictions(self) -> None:
        self.assertAlmostEqual(expected_measured(30), 37.8333, places=4)
        self.assertAlmostEqual(expected_measured(40), 49.3333, places=4)
        self.assertAlmostEqual(expected_measured(50), 60.8333, places=4)


if __name__ == "__main__":
    unittest.main()
