"""Flow measurement helpers for Aichi OF-Z pulse sensors."""

from __future__ import annotations


# Aichi Tokei OF05 series pulse constant (ml per pulse).
DEFAULT_PULSE_ML = 0.46


def pulses_to_ccpm(pulses: float, elapsed_s: float, pulse_ml: float = DEFAULT_PULSE_ML) -> float:
    """Convert counted pulses over a time window to cc/min (ml/min)."""
    if elapsed_s <= 0:
        return 0.0
    # frequency_hz * pulse_ml * 60 = cc/min
    return (pulses / elapsed_s) * pulse_ml * 60.0


def frequency_to_ccpm(frequency_hz: float, pulse_ml: float = DEFAULT_PULSE_ML) -> float:
    return frequency_hz * pulse_ml * 60.0
