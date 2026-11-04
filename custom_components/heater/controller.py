"""Predictive on/off controller for Heat Learner (no Home Assistant imports).

Plain hysteresis thermostats overshoot because the heater keeps radiating
after it is switched off. This controller *learns that overshoot* ("coast")
from every heating cycle and switches the heater off early by exactly that
amount, so the room peaks at the target instead of above it.

    switch ON  when  T <= target - hysteresis
    switch OFF when  T >= target - coast        (coast learned, 0..1.5 °C)

Relay protection: minimum on/off times, except for the hard safety cut-off.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

COAST_MIN = 0.0
COAST_MAX = 1.5
COAST_ALPHA = 0.3  # exponential average weight of a new cycle
TRACK_FALL = 0.15  # temperature drop from the peak that ends a measurement
TRACK_MAX_S = 45 * 60
MIN_ON_FOR_LEARN_S = 180


@dataclass
class ControllerConfig:
    hysteresis: float = 0.3
    min_on_s: float = 180
    min_off_s: float = 180
    safety_margin: float = 1.0  # force off above target + margin


class OnOffController:
    def __init__(self, cfg: ControllerConfig, coast: float = 0.2) -> None:
        self.cfg = cfg
        self.coast = coast
        self.last_change_ts: float | None = None
        self._prev_on: bool | None = None
        self._on_since: float | None = None
        self._tracking = False
        self._off_temp = 0.0
        self._peak = 0.0
        self._off_ts = 0.0
        self._valid_cycle = False

    # --------------------------------------------------------------- decision
    def thresholds(self, target: float) -> tuple[float, float]:
        on_thr = target - self.cfg.hysteresis
        off_thr = max(target - self.coast, on_thr + 0.1)
        return on_thr, off_thr

    def decide(self, temp: float, target: float, heater_on: bool, ts: float) -> bool:
        on_thr, off_thr = self.thresholds(target)
        if temp >= target + self.cfg.safety_margin:
            return False  # hard safety, ignores min times
        want = heater_on
        if temp <= on_thr:
            want = True
        elif temp >= off_thr:
            want = False
        if want != heater_on and self.last_change_ts is not None:
            held = ts - self.last_change_ts
            limit = self.cfg.min_off_s if want else self.cfg.min_on_s
            if held < limit:
                return heater_on
        return want

    def notify_changed(self, ts: float) -> None:
        self.last_change_ts = ts

    # ------------------------------------------------------------ coast learn
    def reset_tracking(self) -> None:
        self._tracking = False
        self._prev_on = None
        self._on_since = None

    def observe(self, temp: float, heater_on: bool, ts: float) -> None:
        """Call once per tick with the *current* heater state."""
        prev = self._prev_on
        self._prev_on = heater_on

        if heater_on:
            if prev is not True:
                self._on_since = ts
            if self._tracking:
                self._finish(temp)
            return

        # heater is off
        if prev is True:  # just switched off
            self._tracking = True
            self._off_temp = temp
            self._peak = temp
            self._off_ts = ts
            self._valid_cycle = (
                self._on_since is not None and ts - self._on_since >= MIN_ON_FOR_LEARN_S
            )
            return
        if not self._tracking:
            return
        self._peak = max(self._peak, temp)
        if temp <= self._peak - TRACK_FALL or ts - self._off_ts > TRACK_MAX_S:
            self._finish(temp)

    def _finish(self, temp: float) -> None:
        self._tracking = False
        if not self._valid_cycle:
            return
        measured = min(max(self._peak - self._off_temp, COAST_MIN), COAST_MAX)
        self.coast = min(
            max((1 - COAST_ALPHA) * self.coast + COAST_ALPHA * measured, COAST_MIN),
            COAST_MAX,
        )

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {"coast": self.coast}

    def load(self, data: dict[str, Any]) -> None:
        try:
            self.coast = min(max(float(data["coast"]), COAST_MIN), COAST_MAX)
        except (KeyError, TypeError, ValueError):
            pass
