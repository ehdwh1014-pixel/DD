"""PI controller with optional first-order low-pass filter and anti-windup."""

from __future__ import annotations

import math


class LowPassFilter:
    """Simple 1st-order LPF. cutoff_hz <= 0 disables filtering."""

    def __init__(self) -> None:
        self.value: float | None = None

    def reset(self, value: float | None = None) -> None:
        self.value = value

    def update(self, sample: float, dt: float, cutoff_hz: float) -> float:
        if self.value is None or cutoff_hz <= 0 or dt <= 0:
            self.value = sample
            return sample
        tau = 1.0 / (2.0 * math.pi * cutoff_hz)
        alpha = dt / (tau + dt)
        self.value += alpha * (sample - self.value)
        return self.value


class PIController:
    """AO = clamp(P * error + I * integral, ao_min, ao_max)."""

    def __init__(self, ao_min: float = 0.0, ao_max: float = 5.0) -> None:
        self.ao_min = ao_min
        self.ao_max = ao_max
        self.integral = 0.0

    def reset(self) -> None:
        self.integral = 0.0

    def update(
        self,
        setpoint: float,
        process_value: float,
        p_gain: float,
        i_gain: float,
        dt: float,
    ) -> float:
        error = setpoint - process_value
        provisional = p_gain * error + i_gain * self.integral
        saturated = provisional <= self.ao_min or provisional >= self.ao_max

        # Anti-windup: only integrate when output is not pushing further into saturation.
        if dt > 0 and not (saturated and error * provisional > 0):
            self.integral += error * dt

        output = p_gain * error + i_gain * self.integral
        return max(self.ao_min, min(self.ao_max, output))
