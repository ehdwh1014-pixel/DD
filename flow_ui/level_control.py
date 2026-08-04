"""Pure level-sensor to valve-command logic."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ValveRole(str, Enum):
    SUPPLY = "급수"
    DRAIN = "배수"


@dataclass(frozen=True)
class ValveDecision:
    opened: bool
    state: str
    fault: bool = False


def decide_valve(role: ValveRole, high: bool, low: bool, previous: bool) -> ValveDecision:
    """Return a fail-safe valve command.

    Neither contact active holds the previous command. Both active is treated
    as an impossible sensor state and closes the valve.
    """
    if high and low:
        return ValveDecision(False, "센서 충돌 · 안전 닫힘", True)
    if not high and not low:
        return ValveDecision(previous, "중간 수위 · 상태 유지")

    if role is ValveRole.SUPPLY:
        opened = low  # low -> fill, high -> stop filling
    else:
        opened = high  # high -> drain, low -> stop draining
    return ValveDecision(opened, "LOW 감지" if low else "HIGH 감지")
