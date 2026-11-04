# Heater for Home Assistant

A self-learning **on/off thermostat for electric heaters**. It watches how your room
heats up and cools down, learns that behaviour, and uses it to hold the target
temperature with a plain on/off switch (smart plug, relay, `input_boolean`, ...).

## What it learns

From your room temperature (and optionally the outdoor temperature) it fits a simple
thermal model with Recursive Least Squares, continuously, so it follows the seasons:

| Learned | Used for |
| --- | --- |
| Heating rate (°C/h, heater on) | "Time to reach target", preheating |
| Heat loss / cooling rate (°C/h, heater off) | maximum reachable temperature, preheating |
| Heater response time (picked from 0–25 min) | accurate model for oil radiators vs. panels |
| Overshoot after switch-off ("coast") | switches off *early* so the room peaks at the target, not above it |

Control rule: **on** at `target − hysteresis`, **off** at `target − learned overshoot`,
with minimum on/off times to protect relays.

## Installation

1. Copy `custom_components/heater` into `<config>/custom_components/`
   (or add the repo to HACS as a custom repository).
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → Heater*.
   Choose the room temperature sensor and the heater switch.
   Optional: outdoor temperature sensor (much better learning) and a window sensor
   (heater is switched off while it is open, and nothing is learned from that period).

## Entities

* `climate.<name>`: the thermostat (Off / Heat, target temperature, preset Eco).
* Sensors: heating rate, cooling rate, **time to reach target**, maximum reachable
  temperature, learned overshoot, learned heater response time, learning progress (%).

## Services

* `heater.set_ready_by`: `time: "07:00:00"`, optional `temperature: 21`.
  Keeps the room at the eco temperature, then starts heating at the *latest* moment
  that still reaches the target at 07:00 (learned heat-up time + 15 % margin).
  Afterwards the ready-by temperature becomes the new target.
* `heater.clear_ready_by`
* `heater.reset_learning`

Example automation (warm bathroom every weekday at 06:30):

```yaml
automation:
  - alias: Bathroom warm at 06:30
    triggers:
      - trigger: time
        at: "21:00:00"
    conditions:
      - condition: time
        weekday: [sun, mon, tue, wed, thu]
    actions:
      - action: heater.set_ready_by
        target:
          entity_id: climate.bathroom
        data:
          time: "06:30:00"
          temperature: 22
```

## Good to know

* **Learning takes time.** It needs both heating and cooling periods. Count on 1–2
  days before the numbers are good; *Learning progress* shows it. Until then it uses
  sensible defaults and still regulates like a normal thermostat.
* The outdoor sensor is optional, but without it the model cannot separate "cold
  outside" from "weak heater", so predictions are rougher.
* **Safety:** if the temperature sensor becomes unavailable (after a 2 min start-up
  grace) or the window is open, the heater is switched off. The heater is also
  switched off when the integration is unloaded or Home Assistant stops. A hard cut-off
  applies at target + 1 °C. This does **not** replace a thermal fuse or the heater's own
  thermostat/over-temperature protection.
* Sensors in °F are converted automatically; the climate entity works in °C.

## Development

`python3 tests/test_simulation.py` runs the learner and controller against a simulated
room (no Home Assistant needed).
