#!/usr/bin/env python3
"""MFC 유량 환산 CLI.

예:
  python -m mfc_calibration.cli expected 30
  python -m mfc_calibration.cli correct 38
  python -m mfc_calibration.cli compensate 30
  python -m mfc_calibration.cli table
"""

from __future__ import annotations

import argparse
import sys

from .conversion import (
    CALIBRATION_POINTS,
    FLOW_INTERCEPT,
    FLOW_SLOPE,
    PREVIOUS_INTERCEPT,
    PREVIOUS_SLOPE,
    compensated_setpoint,
    corrected_from_measured,
    expected_measured,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MFC 설정값↔실측 유량 환산 (y = 1.15x + 3.3333)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    expected = sub.add_parser("expected", help="설정값 → 예상 실측 유량")
    expected.add_argument("setpoint", type=float, help="MFC 설정값 (LPM)")

    correct = sub.add_parser("correct", help="실측 유량 → 보정 환산값")
    correct.add_argument("measured", type=float, help="유량계 실측값 (LPM)")

    compensate = sub.add_parser("compensate", help="목표 실측 → MFC에 넣을 설정값")
    compensate.add_argument("desired", type=float, help="목표 실측 유량 (LPM)")

    sub.add_parser("table", help="환산 표와 캘리브레이션 점 출력")
    return parser


def print_table() -> None:
    print(f"현재 환산식: y = {FLOW_SLOPE}x + {FLOW_INTERCEPT}")
    print(f"이전 환산식: y = {PREVIOUS_SLOPE}x + {PREVIOUS_INTERCEPT}  (미사용)")
    print()
    print("캘리브레이션 점 (설정 → 실측):")
    for setpoint, measured in CALIBRATION_POINTS:
        pred = expected_measured(setpoint)
        print(
            f"  설정 {setpoint:5.1f} → 실측 {measured:5.1f} "
            f"(식 예측 {pred:5.2f}, 오차 {pred - measured:+.2f})"
        )
    print()
    print(f"{'목표실측':>8} {'넣을설정':>10} {'설정시예상실측':>14} {'실측보정값':>10}")
    for value in (5, 10, 20, 30, 40, 50, 60):
        command = compensated_setpoint(value)
        predicted = expected_measured(value)
        corrected = corrected_from_measured(value)
        print(
            f"{value:8.1f} {command:10.3f} {predicted:14.3f} {corrected:10.3f}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "expected":
        print(f"{expected_measured(args.setpoint):.4f}")
        return 0
    if args.command == "correct":
        print(f"{corrected_from_measured(args.measured):.4f}")
        return 0
    if args.command == "compensate":
        print(f"{compensated_setpoint(args.desired):.4f}")
        return 0
    if args.command == "table":
        print_table()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
