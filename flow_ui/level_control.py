"""Pure level-sensor to valve-command logic."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ValveRole(str, Enum):
    SUPPLY = "급수"
    DRAIN = "배수"


class SensorType(str, Enum):
    TWO_POINT = "2접점"
    FOUR_POINT = "4접점"


@dataclass(frozen=True)
class ValveDecision:
    opened: bool
    state: str
    fault: bool = False


def decide_valve(
    role: ValveRole,
    high: bool,
    low: bool,
    previous: bool,
    sensor_type: SensorType = SensorType.TWO_POINT,
) -> ValveDecision:
    """Return a fail-safe valve command.

    2접점 (independent HIGH/LOW):
      Both active = impossible sensor state → close.
      Neither active = hold previous.

    4접점 (sequential reed switch, e.g. DFR type):
      H and L are sequential: when water rises past H, both H and L are ON.
      H ON = high water (close supply).
      Only L ON (H OFF) = low water (open supply).
      Neither ON = very low, open supply.
      For drain: H ON = open drain, H OFF + L ON or neither = close drain.
    """
    if sensor_type is SensorType.FOUR_POINT:
        if high:
            if role is ValveRole.SUPPLY:
                return ValveDecision(False, "H 감지 · 급수 닫힘")
            else:
                return ValveDecision(True, "H 감지 · 배수 열림")
        if low:
            if role is ValveRole.SUPPLY:
                return ValveDecision(True, "L 감지 · 급수 열림")
            else:
                return ValveDecision(False, "L 감지 · 배수 닫힘")
        if role is ValveRole.SUPPLY:
            return ValveDecision(True, "센서 OFF · 급수 열림")
        else:
            return ValveDecision(False, "센서 OFF · 배수 닫힘")

    # 2접점 (original logic)
    if high and low:
        return ValveDecision(False, "센서 충돌 · 안전 닫힘", True)
    if not high and not low:
        return ValveDecision(previous, "중간 수위 · 상태 유지")

    if role is ValveRole.SUPPLY:
        opened = low  # low -> fill, high -> stop filling
    else:
        opened = high  # high -> drain, low -> stop draining
    return ValveDecision(opened, "LOW 감지" if low else "HIGH 감지")
