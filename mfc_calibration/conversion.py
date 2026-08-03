"""MFC 설정값과 유량계 실측값 환산.

실측 캘리브레이션 (그래프 LPM):
  설정 30 → 실측 38
  설정 40 → 실측 49
  설정 50 → 실측 61

선형 회귀 (R² ≈ 0.9994):
  실측유량 y = 1.15 * 설정값 x + 3.3333

이전 환산식 y = 0.9337x + 4.6452 는 위 실측과 맞지 않아 교체함.
"""

from __future__ import annotations

# 그래프/실측 기준 환산식: y(실측) = slope * x(설정) + intercept
FLOW_SLOPE = 1.15
FLOW_INTERCEPT = 3.3333

# 이전에 쓰이던 환산식 (참고용, 더 이상 사용하지 않음)
PREVIOUS_SLOPE = 0.9337
PREVIOUS_INTERCEPT = 4.6452

# 캘리브레이션에 사용한 설정→실측 점 (LPM)
CALIBRATION_POINTS: tuple[tuple[float, float], ...] = (
    (30.0, 38.0),
    (40.0, 49.0),
    (50.0, 61.0),
)


def expected_measured(setpoint: float) -> float:
    """MFC 설정값 x → 예상 실측 유량 y."""
    return FLOW_SLOPE * setpoint + FLOW_INTERCEPT


def corrected_from_measured(measured: float) -> float:
    """유량계 실측 y → 보정된 설정 환산값 x = (y - b) / a."""
    return (measured - FLOW_INTERCEPT) / FLOW_SLOPE


def compensated_setpoint(desired_actual: float) -> float:
    """목표 실측 유량을 얻기 위해 MFC에 넣어야 할 설정값."""
    return (desired_actual - FLOW_INTERCEPT) / FLOW_SLOPE
