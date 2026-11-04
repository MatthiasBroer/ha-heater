"""Thermal model learning for Heat Learner.

Pure Python (no Home Assistant imports) so it can be tested standalone.

Model (first order room):

    dT/dt = kh * u  -  kl * (T - T_out)            (with outdoor sensor)
    dT/dt = kh * u  -  kl * (T - 20)  +  c         (without outdoor sensor)

    T      room temperature  [°C]
    u      heater duty (0..1) over the observation window
    kh     heating rate with heater fully on      [°C/h]
    kl     heat loss coefficient                  [1/h]

The parameters are estimated online with Recursive Least Squares (RLS)
with a forgetting factor, so the model follows the seasons and changes in
the room (open doors, new insulation, ...).
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any

WINDOW_S = 600  # length of one learning observation window
MAX_GAP_S = 150  # a gap longer than this invalidates the running window
OUTLIER_RATE = 15.0  # °C/h prediction error above which a sample is ignored
P_MAX = 1e3  # covariance wind-up guard
PROGRESS_TARGET = 24  # windows with heating AND with cooling for 100 %

PRIOR_KH = 1.5
PRIOR_KL = 0.10
PRIOR_AMBIENT = 12.0  # assumed ambient for the no-outdoor-sensor prior
T_REF = 20.0  # no-outdoor model is centred on this temperature (conditioning)
MAX_REJECTS = 12  # consecutive outliers after which data is accepted anyway
LAG_CANDIDATES_S = (0, 120, 300, 600, 900, 1500)  # heater response times tried
DEFAULT_LAG_S = 300
ERR_ALPHA = 0.03  # smoothing of the per-lag prediction error
MIN_SAMPLES_SELECT = 24  # windows before the best lag is trusted


class ThermalModel:
    """RLS estimator of the room's heating/cooling behaviour."""

    def __init__(self, use_outdoor: bool, forget: float = 0.999) -> None:
        self.use_outdoor = use_outdoor
        self.forget = forget
        self.n_samples = 0
        self.n_heat = 0  # windows with mostly heating
        self.n_cool = 0  # windows with mostly cooling
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self) -> None:
        if self.use_outdoor:
            self.theta = [PRIOR_KH, PRIOR_KL]
            self.P = [[10.0, 0.0], [0.0, 0.5]]
        else:
            self.theta = [PRIOR_KH, PRIOR_KL, PRIOR_KL * (PRIOR_AMBIENT - T_REF)]
            self.P = [[10.0, 0.0, 0.0], [0.0, 0.5, 0.0], [0.0, 0.0, 4.0]]
        self._rejects = 0
        self.n_samples = 0
        self.n_heat = 0
        self.n_cool = 0

    @property
    def kh(self) -> float:
        return self.theta[0]

    @property
    def kl(self) -> float:
        return self.theta[1]

    # --------------------------------------------------------------- learning
    def _regressors(self, t_mean: float, t_out: float | None, u: float) -> list[float]:
        if self.use_outdoor:
            return [u, -(t_mean - float(t_out))]
        return [u, -(t_mean - T_REF), 1.0]

    def update(
        self, t_mean: float, t_out: float | None, u: float, rate: float
    ) -> bool:
        """Feed one observation: mean temp, outdoor temp, duty, measured dT/dt."""
        if self.use_outdoor and t_out is None:
            return False
        x = self._regressors(t_mean, t_out, u)
        n = len(x)
        err = rate - sum(self.theta[i] * x[i] for i in range(n))
        if self.n_samples > 20 and abs(err) > OUTLIER_RATE:
            self._rejects += 1
            if self._rejects < MAX_REJECTS:
                return False  # a real change keeps repeating -> accept it
        self._rejects = 0

        lam = self.forget
        px = [sum(self.P[i][j] * x[j] for j in range(n)) for i in range(n)]
        denom = lam + sum(x[i] * px[i] for i in range(n))
        if denom <= 1e-9:
            return False
        k = [p / denom for p in px]
        for i in range(n):
            self.theta[i] += k[i] * err
        for i in range(n):
            for j in range(n):
                self.P[i][j] = (self.P[i][j] - k[i] * px[j]) / lam
        # symmetrise
        for i in range(n):
            for j in range(i + 1, n):
                avg = 0.5 * (self.P[i][j] + self.P[j][i])
                self.P[i][j] = self.P[j][i] = avg
        # covariance wind-up guard (no excitation -> P grows with forgetting)
        biggest = max(self.P[i][i] for i in range(n))
        if biggest > P_MAX:
            scale = P_MAX / biggest
            self.P = [[v * scale for v in row] for row in self.P]

        # physical bounds
        self.theta[0] = min(max(self.theta[0], 0.05), 30.0)
        self.theta[1] = min(max(self.theta[1], 0.003), 3.0)
        if not self.use_outdoor:
            self.theta[2] = min(max(self.theta[2], -60.0), 60.0)

        self.n_samples += 1
        if u >= 0.7:
            self.n_heat += 1
        elif u <= 0.3:
            self.n_cool += 1
        return True

    # ------------------------------------------------------------ predictions
    @property
    def progress(self) -> float:
        """Learning progress 0..1 (needs both heating and cooling data)."""
        return min(1.0, min(self.n_heat, self.n_cool) / PROGRESS_TARGET)

    def rate(self, t: float, t_out: float | None, u: float) -> float:
        """Predicted dT/dt [°C/h] at temperature t with duty u."""
        if self.use_outdoor:
            ref = t_out if t_out is not None else PRIOR_AMBIENT
            return self.theta[0] * u - self.theta[1] * (t - ref)
        return self.theta[0] * u - self.theta[1] * (t - T_REF) + self.theta[2]

    def equilibrium(self, t_out: float | None, u: float) -> float:
        """Temperature the room settles at for constant duty u."""
        kh, kl = self.theta[0], self.theta[1]
        if self.use_outdoor:
            ref = t_out if t_out is not None else PRIOR_AMBIENT
            return ref + kh * u / kl
        return T_REF + (kh * u + self.theta[2]) / kl

    def time_to_reach(
        self, t_now: float, target: float, t_out: float | None
    ) -> float | None:
        """Hours of continuous heating to reach target. None = unreachable."""
        if t_now >= target:
            return 0.0
        t_eq = self.equilibrium(t_out, 1.0)
        if t_eq <= target + 0.05:
            return None
        hours = math.log((t_eq - t_now) / (t_eq - target)) / self.theta[1]
        if not math.isfinite(hours) or hours > 48:
            return None
        return max(hours, 0.0)

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "use_outdoor": self.use_outdoor,
            "theta": self.theta,
            "P": self.P,
            "n_samples": self.n_samples,
            "n_heat": self.n_heat,
            "n_cool": self.n_cool,
        }

    def load(self, data: dict[str, Any]) -> None:
        try:
            if bool(data.get("use_outdoor")) != self.use_outdoor:
                return  # sensor setup changed -> start fresh
            theta = [float(v) for v in data["theta"]]
            p = [[float(v) for v in row] for row in data["P"]]
            if len(theta) != len(self.theta) or not all(map(math.isfinite, theta)):
                return
            self.theta, self.P = theta, p
            self.n_samples = int(data.get("n_samples", 0))
            self.n_heat = int(data.get("n_heat", 0))
            self.n_cool = int(data.get("n_cool", 0))
        except (KeyError, TypeError, ValueError):
            self.reset()


class Learner:
    """Turns raw ticks (temp, heater state) into model observations.

    A heater does not deliver its heat instantly: an oil radiator keeps
    warming the room for many minutes after it is switched off, an infrared
    panel hardly does. That response time matters a lot for the fit, and it
    is different for every heater, so the learner runs one thermal model per
    candidate lag in parallel and uses the one that predicts best.
    """

    def __init__(self, use_outdoor: bool, window_s: int = WINDOW_S) -> None:
        self.use_outdoor = use_outdoor
        self.window_s = window_s
        self.lags = list(LAG_CANDIDATES_S)
        self.models = [ThermalModel(use_outdoor) for _ in self.lags]
        self._temps: deque[float] = deque(maxlen=4)
        self._last_ts: float | None = None
        self._p = [0.0] * len(self.lags)
        self._err: list[float | None] = [None] * len(self.lags)
        self._restart()

    def _restart(self) -> None:
        self._start_ts: float | None = None
        self._start_temp: float | None = None
        self._u_s = [0.0] * len(self.lags)
        self._outdoor: list[float] = []

    def reset(self) -> None:
        for m in self.models:
            m.reset()
        self._p = [0.0] * len(self.lags)
        self._err = [None] * len(self.lags)
        self._restart()

    # ---------------------------------------------------------------- results
    @property
    def best_index(self) -> int:
        default = self.lags.index(DEFAULT_LAG_S)
        if min(m.n_samples for m in self.models) < MIN_SAMPLES_SELECT:
            return default
        scored = [(e, i) for i, e in enumerate(self._err) if e is not None]
        return min(scored)[1] if scored else default

    @property
    def model(self) -> ThermalModel:
        return self.models[self.best_index]

    @property
    def heater_lag_s(self) -> int:
        return self.lags[self.best_index]

    # ------------------------------------------------------------------- tick
    def tick(
        self,
        ts: float,
        temp: float | None,
        heater_on: bool,
        outdoor: float | None,
        valid: bool = True,
    ) -> bool:
        """Process one tick. ``heater_on`` is the state during the interval
        since the previous tick. Returns True when the models were updated."""
        last, self._last_ts = self._last_ts, ts
        if temp is None:
            self._restart()
            return False
        self._temps.append(temp)
        dt = 0.0 if last is None else ts - last
        if dt > MAX_GAP_S or not valid:
            self._restart()
            return False

        if self._start_ts is None:
            self._start_ts = ts
            self._start_temp = sum(self._temps) / len(self._temps)
            return False

        # Effective heater output follows the switch with a first-order lag.
        target_p = 1.0 if heater_on else 0.0
        for i, lag in enumerate(self.lags):
            if lag > 0:
                self._p[i] += (target_p - self._p[i]) * (1.0 - math.exp(-dt / lag))
            else:
                self._p[i] = target_p
            self._u_s[i] += self._p[i] * dt
        if outdoor is not None:
            self._outdoor.append(outdoor)

        elapsed = ts - self._start_ts
        if elapsed < self.window_s:
            return False

        t_end = sum(self._temps) / len(self._temps)
        t_start = self._start_temp
        assert t_start is not None
        updated = False
        if not self.use_outdoor or self._outdoor:
            rate = (t_end - t_start) / (elapsed / 3600.0)
            t_mean = (t_start + t_end) / 2.0
            t_out = sum(self._outdoor) / len(self._outdoor) if self._outdoor else None
            for i, model in enumerate(self.models):
                u = min(1.0, self._u_s[i] / elapsed)
                err = rate - model.rate(t_mean, t_out, u)  # a-priori error
                if abs(err) < OUTLIER_RATE:
                    prev = self._err[i]
                    sq = err * err
                    self._err[i] = sq if prev is None else prev + ERR_ALPHA * (sq - prev)
                updated |= model.update(t_mean, t_out, u, rate)
        # chain the next window from this one
        self._start_ts = ts
        self._start_temp = t_end
        self._u_s = [0.0] * len(self.lags)
        self._outdoor = []
        return updated

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict[str, Any]:
        return {
            "use_outdoor": self.use_outdoor,
            "lags": self.lags,
            "err": self._err,
            "models": [m.to_dict() for m in self.models],
        }

    def load(self, data: dict[str, Any]) -> None:
        try:
            if bool(data["use_outdoor"]) != self.use_outdoor or list(data["lags"]) != self.lags:
                return
            for m, md in zip(self.models, data["models"]):
                m.load(md)
            self._err = [None if e is None else float(e) for e in data["err"]]
        except (KeyError, TypeError, ValueError):
            self.reset()
