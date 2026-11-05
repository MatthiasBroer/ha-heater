# Heat Learner for Home Assistant

A self-learning **on/off thermostat for electric heaters**. It watches how your room
heats up and cools down, learns that behaviour, and uses it to hold the target
temperature with a plain on/off switch (smart plug, relay, `switch`, `input_boolean`, ...).
It also has a weekly comfort schedule with automatic preheating.

The integration domain is `heater`; in the UI it is called **Heat Learner**.

## What it learns

From your room temperature (and optionally the outdoor temperature) it fits a simple
thermal model with Recursive Least Squares (10-minute observation windows), continuously,
so it follows the seasons. Everything learned is stored and survives restarts.

| Learned                                      | Used for                                                           |
| -------------------------------------------- | ------------------------------------------------------------------ |
| Heating rate (°C/h, heater on)               | "Time to reach target", preheating                                 |
| Heat loss / cooling rate (°C/h, heater off)  | Maximum reachable temperature, preheating                          |
| Heater response time (best of 0 to 25 min)   | Accurate model for oil radiators vs. panels                        |
| Overshoot after switch-off ("coast", 0-1.5 °C) | Switches off *early* so the room peaks at the target, not above it |

Control rule: **on** at `target - hysteresis`, **off** at `target - learned overshoot`,
with minimum on/off times to protect relays. The controller runs every 30 seconds.

## Installation

1. Copy `custom_components/heater` into `<config>/custom_components/`
   (or add the repo to HACS as a custom repository).
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → Heater*.

Setup asks for:

| Field                        | Required | Notes                                                                 |
| ---------------------------- | -------- | --------------------------------------------------------------------- |
| Name                         | yes      |                                                                       |
| Room temperature sensor      | yes      | A `sensor` with device class temperature                              |
| Heater switch                | yes      | A `switch` or `input_boolean`. One heater can only be used once.      |
| Outdoor temperature sensor   | no       | Much better learning                                                  |
| Window/door sensor           | no       | Heater is switched off while open, and nothing is learned meanwhile   |

## Options

Open *Settings → Devices & services → Heater → Configure*. You get a menu:

* **Thermostat settings**
  * Hysteresis (default 0.3 °C): the heater switches on this far below the target.
  * Minimum on time / minimum off time (default 3 min each).
  * Eco temperature (default 17 °C).
  * Minimum and maximum target (defaults 7 °C and 28 °C).
  * *External physical climate device* (optional): Heat Learner follows target-temperature
    changes made on that climate entity. It never controls that device.
* **Add schedule period** and **Remove schedule period**: see below.
* **Save and close**: stores the options and reloads the integration.

## Weekly schedule

A schedule is a list of *comfort periods*. Each period has one or more weekdays, a start
time, an end time and a target temperature.

* Inside a comfort period the room is held at that period's temperature.
* Outside comfort periods the room is held at the **eco temperature**, and Heat Learner
  **preheats** so the next comfort period starts warm. The start moment is calculated from
  the learned heat-up time (plus 15 % margin and 5 minutes).
* Periods on the same day may not overlap, and a period must end later than it starts
  (it cannot cross midnight; use two periods instead).
* **Manual changes:** if you change the target while a period is active, your value is kept
  until that period ends. If you change it outside a period, it is kept until the next
  period starts. After that the schedule takes over again.
* Without any schedule, the climate entity simply uses the target you set (or the eco
  temperature when the Eco preset is selected).

## Entities

* `climate.<name>`: the thermostat.
  * Modes: Off / Heat. Presets: none / Eco. Target step 0.5 °C, always in °C.
  * Becomes *unavailable* while the room temperature sensor is unavailable.
  * Attributes: `effective_target`, `window_open`, `ready_by`, `ready_temperature`,
    `preheat_starts_at`, `preheating`, `minutes_to_target`, `learning_progress`,
    `learned_coast`, `learned_heater_lag_min`.
* Sensors:
  * **Status**: what the heater is doing right now (off, heating, preheating, idle,
    waiting for minimum on/off time, window open, temperature sensor unavailable).
    Attributes `target_source` (schedule, manual override, preheat, eco, ...),
    `target_until` and `effective_target` explain *why* the target is what it is.
  * Heating rate, cooling rate, **time to reach target**.
  * Diagnostic: maximum reachable temperature, learned overshoot, learned heater
    response time, learning progress (%).

## Services

All services target a Heat Learner `climate` entity.

* `heater.set_ready_by`: `time: "07:00:00"`, optional `temperature: 21`.
  Keeps the room at the eco temperature, then starts heating at the *latest* moment
  that still reaches the target at the next occurrence of that time (learned heat-up
  time + 15 % margin + 5 minutes). If the heat-up time is not known yet it starts
  3 hours early; the lead time is capped at 12 hours. Afterwards the ready-by
  temperature becomes the new target. Without `temperature` the current target is used.
* `heater.clear_ready_by`: cancel a pending preheat.
* `heater.reset_learning`: forget everything learned about the room and start over.

For regular times, use the weekly schedule instead of automations. For one-off or
conditional cases, an automation works well:

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

* **Learning takes time.** It needs both heating and cooling periods. Count on 1-2
  days before the numbers are good; *Learning progress* shows it. Until then it uses
  sensible defaults and still regulates like a normal thermostat.
* The outdoor sensor is optional, but without it the model cannot separate "cold
  outside" from "weak heater", so predictions are rougher.
* Time spent with the window open is not used for learning.
* Changing the outdoor sensor setup (adding or removing it) starts the model fresh.
* Sensors in °F are converted automatically; the climate entity works in °C.
* Translations: English and Dutch.

## Safety

* If the room temperature sensor becomes unavailable (after a 2 minute start-up grace)
  while in Heat mode, the heater is switched off.
* The heater is switched off while the window sensor reports open.
* The heater is switched off when switching to Off, when the integration is unloaded
  or reloaded, and when Home Assistant stops.
* A hard cut-off applies at target + 1 °C and ignores the minimum on/off times.

This does **not** replace a thermal fuse or the heater's own thermostat and
over-temperature protection. If Home Assistant crashes while the heater is on,
nothing can switch it off.

## Development

`python3 tests/test_simulation.py` runs the learner and controller against a simulated
room (no Home Assistant needed). `learner.py`, `controller.py` and `schedule.py` contain
no Home Assistant imports; `manager.py` connects them to Home Assistant.