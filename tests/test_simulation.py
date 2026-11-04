"""Standalone simulation of a room: checks learning accuracy + control quality.

Run:  python3 tests/test_simulation.py      (no Home Assistant needed)
"""
from __future__ import annotations

import importlib.util
import math
import random
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "custom_components" / "heat_learner"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, BASE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


learner_mod = load("learner")
ctrl_mod = load("controller")

KH_TRUE = 2.4  # °C/h with heater fully on
KL_TRUE = 0.10  # 1/h  (time constant 10 h)
HEATER_LAG_S = 8 * 60  # radiator keeps heating after switch-off


def simulate(use_outdoor: bool, days: float = 6, seed: int = 1, kh=KH_TRUE, lag_s=HEATER_LAG_S, learn_coast=True):
    rnd = random.Random(seed)
    learner = learner_mod.Learner(use_outdoor)
    ctrl = ctrl_mod.OnOffController(ctrl_mod.ControllerConfig(), coast=0.0 if not learn_coast else 0.2)
    T, p, on = 17.0, 0.0, False
    dt = 30
    target = 21.0
    ts = 0.0
    devs, peaks = [], []
    cycles = 0
    steps = int(days * 86400 / dt)
    for i in range(steps):
        ts += dt
        t_out = 5 + 3 * math.sin(2 * math.pi * ts / 86400)
        # heater element with thermal lag
        p += ((1.0 if on else 0.0) - p) * dt / lag_s
        T += (kh * p - KL_TRUE * (T - t_out)) * dt / 3600
        reading = round(T + rnd.gauss(0, 0.03), 1)  # 0.1 °C sensor
        out_reading = round(t_out, 1)

        learner.tick(ts, reading, on, out_reading if use_outdoor else None)
        want = ctrl.decide(reading, target, on, ts)
        if want != on:
            on = want
            ctrl.notify_changed(ts)
            cycles += 1
        if learn_coast:
            ctrl.observe(reading, on, ts)

        if ts > 2 * 86400:  # evaluate after 2 days of learning
            devs.append(T - target)
            peaks.append(T)
    m = learner.model
    print(f"selected heater lag: {learner.heater_lag_s/60:.0f} min (true {lag_s/60:.0f} min)")
    return m, ctrl, devs, cycles, ts


def check(use_outdoor: bool) -> bool:
    m, ctrl, devs, cycles, ts = simulate(use_outdoor)
    mean_dev = sum(devs) / len(devs)
    max_dev = max(devs)
    min_dev = min(devs)
    print(f"--- use_outdoor={use_outdoor}")
    print(f"learned kh={m.kh:.2f} (true {KH_TRUE}), kl={m.kl:.3f} (true {KL_TRUE})")
    print(f"learned coast={ctrl.coast:.2f} °C, progress={m.progress*100:.0f} %")
    print(
        f"after 2 days: mean dev {mean_dev:+.2f}, range [{min_dev:+.2f}, {max_dev:+.2f}] °C,"
        f" switch actions/day {cycles/ (ts/86400):.0f}"
    )
    t_out = 5.0
    est = m.time_to_reach(17.0, 21.0, t_out)
    if est is None:
        print("time_to_reach returned None")
        print("FAIL")
        return False
    true_eq = t_out + KH_TRUE / KL_TRUE
    true_t = math.log((true_eq - 17) / (true_eq - 21)) / KL_TRUE
    print(f"time 17→21 °C: learned {est*60:.0f} min, true {true_t*60:.0f} min")
    ok = True
    ok &= abs(m.kh - KH_TRUE) / KH_TRUE < 0.30
    ok &= abs(m.kl - KL_TRUE) / KL_TRUE < 0.35
    ok &= abs(est - true_t) / true_t < 0.25
    ok &= abs(mean_dev) < 0.35 and max_dev < 0.8 and min_dev > -0.8
    print("PASS" if ok else "FAIL")
    return bool(ok)


def check_overshoot_learning():
    """Slow, powerful heater (oil radiator): learned coast must cut the overshoot."""
    kw = dict(use_outdoor=True, days=6, kh=5.0, lag_s=25 * 60)
    _, c0, d0, *_ = simulate(learn_coast=False, **kw)
    _, c1, d1, *_ = simulate(learn_coast=True, **kw)
    print(f"--- slow heater: peak overshoot without coast learning {max(d0):+.2f} °C,"
          f" with learning {max(d1):+.2f} °C (learned coast {c1.coast:.2f} °C)")
    return c1.coast > 0.05 and max(d1) < max(d0) - 0.05


if __name__ == "__main__":
    results = [check(True), check(False), check_overshoot_learning()]
    sys.exit(0 if all(results) else 1)
