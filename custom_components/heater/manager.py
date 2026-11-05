"""Runtime brain of a Heat Learner config entry.

Reads the sensors every TICK_SECONDS, feeds the learner, runs the on/off
controller, switches the heater and tells the entities to refresh.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.climate import PRESET_ECO, PRESET_NONE, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_ECO_TEMP,
    CONF_HEATER,
    CONF_HYSTERESIS,
    CONF_MAX_TEMP,
    CONF_MIN_OFF,
    CONF_MIN_ON,
    CONF_MIN_TEMP,
    CONF_OUTDOOR_SENSOR,
    CONF_REFERENCE_CLIMATE,
    CONF_TEMP_SENSOR,
    CONF_WINDOW_SENSOR,
    DEFAULT_ECO_TEMP,
    DEFAULT_HYSTERESIS,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_OFF,
    DEFAULT_MIN_ON,
    DEFAULT_MIN_TEMP,
    DEFAULT_TARGET,
    DOMAIN,
    READY_EXTRA_S,
    READY_MARGIN,
    READY_MAX_LEAD_S,
    READY_UNKNOWN_LEAD_S,
    SAVE_DELAY_S,
    STARTUP_GRACE_S,
    STORAGE_VERSION,
    TICK_SECONDS,
)
from .controller import ControllerConfig, OnOffController
from .learner import Learner
from .schedule import active_period, next_period

_LOGGER = logging.getLogger(__name__)


class HeatLearnerManager:
    """Owns learner, controller and state of one room."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        conf: dict[str, Any] = {**entry.data, **entry.options}

        self.temp_entity: str = conf[CONF_TEMP_SENSOR]
        self.heater_entity: str = conf[CONF_HEATER]
        self.outdoor_entity: str | None = conf.get(CONF_OUTDOOR_SENSOR)
        self.window_entity: str | None = conf.get(CONF_WINDOW_SENSOR)
        self.reference_climate_entity: str | None = conf.get(CONF_REFERENCE_CLIMATE)

        self.eco_temp: float = float(conf.get(CONF_ECO_TEMP, DEFAULT_ECO_TEMP))
        self.min_temp: float = float(conf.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        self.max_temp: float = float(conf.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))

        self.learner = Learner(use_outdoor=bool(self.outdoor_entity))
        self.controller = OnOffController(
            ControllerConfig(
                hysteresis=float(conf.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS)),
                min_on_s=float(conf.get(CONF_MIN_ON, DEFAULT_MIN_ON)) * 60,
                min_off_s=float(conf.get(CONF_MIN_OFF, DEFAULT_MIN_OFF)) * 60,
            )
        )

        # User-facing state (persisted).
        self.hvac_mode: HVACMode = HVACMode.OFF
        self.target: float = DEFAULT_TARGET
        self.preset: str = PRESET_NONE
        self.ready_by: datetime | None = None
        self.ready_target: float | None = None
        self._schedule_ready_key: tuple | None = None
        self._manual_override_until: datetime | None = None
        self._manual_override_target: float | None = None

        # Live values.
        self.current_temp: float | None = None
        self.outdoor_temp: float | None = None
        self._last_outdoor: float | None = None
        self.heater_on: bool = False
        self.window_open: bool = False
        self.sensor_ok: bool = True
        self.effective_target: float = self.target
        self.ready_start: datetime | None = None
        self._ready_started = False

        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsub: list[CALLBACK_TYPE] = []
        self._start_ts: float = 0.0
        self.signal = f"{DOMAIN}_update_{entry.entry_id}"
        
        self.target_source: str = "manual"
        self.target_until: datetime | None = None
        self._holding: bool = False  # controller wants a change but min on/off time blocks it

    # ------------------------------------------------------------------ setup
    async def async_start(self) -> None:
        data = await self._store.async_load()
        if data:
            self._restore(data)

        # Set this before the initial reference-climate sync: applying its
        # setpoint runs a tick, which may use the startup-grace fail-safe.
        self._start_ts = dt_util.utcnow().timestamp()
        self._unsub.append(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=TICK_SECONDS)
            )
        )
        self._unsub.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_on_ha_stop
            )
        )

        if self.reference_climate_entity:
            self._unsub.append(
                async_track_state_change_event(
                    self.hass,
                    [self.reference_climate_entity],
                    self._async_reference_climate_changed,
                )
            )
            # Read the current setpoint as well as listening for future changes.
            await self._async_sync_reference_climate()

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        await self._store.async_save(self._data_to_save())
        if self.hvac_mode == HVACMode.HEAT:
            await self._set_heater(False)  # Fail-safe: never leave a heater unsupervised.

    async def _async_on_ha_stop(self, _event: Event) -> None:
        await self._store.async_save(self._data_to_save())
        if self.hvac_mode == HVACMode.HEAT:
            await self._set_heater(False)

    # ------------------------------------------------------------ persistence
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "hvac_mode": self.hvac_mode.value,
            "target": self.target,
            "preset": self.preset,
            "ready_by": self.ready_by.isoformat() if self.ready_by else None,
            "ready_target": self.ready_target,
            "learner": self.learner.to_dict(),
            "controller": self.controller.to_dict(),
            "manual_override_until": (
                self._manual_override_until.isoformat()
                if self._manual_override_until
                else None
            ),
            "manual_override_target": self._manual_override_target,
        }

    def _restore(self, data: dict[str, Any]) -> None:
        try:
            self.hvac_mode = HVACMode(data.get("hvac_mode", HVACMode.OFF))
            self.target = float(data.get("target", DEFAULT_TARGET))
            self.preset = data.get("preset", PRESET_NONE)
            if data.get("manual_override_until"):
                self._manual_override_until = dt_util.parse_datetime(
                    data["manual_override_until"]
                )
                saved_override = data.get("manual_override_target")
                self._manual_override_target = (
                    float(saved_override) if saved_override is not None else None
                )
            if data.get("ready_by"):
                self.ready_by = dt_util.parse_datetime(data["ready_by"])
                self.ready_target = data.get("ready_target")
            self.learner.load(data.get("learner", {}))
            self.controller.load(data.get("controller", {}))
        except (ValueError, TypeError):
            _LOGGER.warning("Could not fully restore saved state, using defaults")

    @callback
    def _request_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY_S)

    # ------------------------------------------------------------- inputs
    def _temp_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                value = TemperatureConverter.convert(
                    value, unit, UnitOfTemperature.CELSIUS
                )
            except (ValueError, TypeError):
                return None
        return value

    def _read_inputs(self) -> None:
        self.current_temp = self._temp_state(self.temp_entity)
        self.outdoor_temp = self._temp_state(self.outdoor_entity)
        if self.outdoor_temp is not None:
            self._last_outdoor = self.outdoor_temp
        heater = self.hass.states.get(self.heater_entity)
        self.heater_on = heater is not None and heater.state == STATE_ON
        if self.window_entity:
            window = self.hass.states.get(self.window_entity)
            self.window_open = window is not None and window.state == STATE_ON
        else:
            self.window_open = False

    # --------------------------------------------- reference climate setpoint
    async def _async_sync_reference_climate(self) -> None:
        """Apply the reference climate's currently selected target, if available."""
        if not self.reference_climate_entity:
            return
        state = self.hass.states.get(self.reference_climate_entity)
        if state is not None:
            await self._async_apply_reference_climate_state(state)

    async def _async_reference_climate_changed(self, event: Event) -> None:
        """React only to a change in the reference climate's target setpoint."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return

        new_target = new_state.attributes.get("temperature")
        old_target = (
            old_state.attributes.get("temperature") if old_state is not None else None
        )
        # Ignore room-temperature, HVAC-mode and other unrelated state updates.
        if new_target is None or new_target == old_target:
            return

        await self._async_apply_reference_climate_state(new_state)

    async def _async_apply_reference_climate_state(self, state: Any) -> None:
        """Validate and convert a climate entity's target to Heat Learner's °C."""
        raw_target = state.attributes.get("temperature")
        if raw_target is None:
            return
        try:
            target = float(raw_target)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Ignoring invalid target temperature from %s: %r",
                self.reference_climate_entity,
                raw_target,
            )
            return

        # Climate entities normally use the HA configured temperature unit.
        # Prefer an entity-provided unit when available.
        unit = (
            state.attributes.get("unit_of_measurement")
            or state.attributes.get("temperature_unit")
            or self.hass.config.units.temperature_unit
        )
        if unit != UnitOfTemperature.CELSIUS:
            try:
                target = TemperatureConverter.convert(
                    target, unit, UnitOfTemperature.CELSIUS
                )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Cannot convert reference climate temperature unit %s for %s",
                    unit,
                    self.reference_climate_entity,
                )
                return

        target = min(max(target, self.min_temp), self.max_temp)
        _LOGGER.debug(
            "Following reference climate %s target: %.1f °C",
            self.reference_climate_entity,
            target,
        )
        await self.async_set_target(target)

    # ---------------------------------------------------------------- control
    async def _async_tick(self, now: datetime | None = None) -> None:
        now = now or dt_util.utcnow()
        ts = now.timestamp()
        self._read_inputs()
        temp = self.current_temp
        self.sensor_ok = temp is not None

        # Learn using the heater state read before any action.
        if self.learner.tick(
            ts, temp, self.heater_on, self.outdoor_temp, valid=not self.window_open
        ):
            self._request_save()

        self.effective_target = self._update_schedule(now)
        self.target_source, self.target_until = self._explain_target(now)
        self._holding = False

        if self.hvac_mode == HVACMode.HEAT:
            await self._async_control(ts, temp)
        else:
            self.controller.reset_tracking()

        async_dispatcher_send(self.hass, self.signal)

    async def _async_control(self, ts: float, temp: float | None) -> None:
        if temp is None:
            if ts - self._start_ts > STARTUP_GRACE_S and self.heater_on:
                _LOGGER.warning("Temperature sensor unavailable, switching heater off")
                await self._set_heater(False, ts)
            self.controller.reset_tracking()
            return
        if self.window_open:
            if self.heater_on:
                await self._set_heater(False, ts)
            self.controller.reset_tracking()
            return

        want = self.controller.decide(temp, self.effective_target, self.heater_on, ts)
        # Did the controller *want* to change but was held back by min on/off time?
        on_thr, off_thr = self.controller.thresholds(self.effective_target)
        raw = True if temp <= on_thr else False if temp >= off_thr else self.heater_on
        self._holding = raw != self.heater_on and want == self.heater_on
        if want != self.heater_on:
            await self._set_heater(want, ts)
        self.controller.observe(temp, self.heater_on, ts)

    async def _set_heater(self, on: bool, ts: float | None = None) -> None:
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "turn_on" if on else "turn_off",
                {"entity_id": self.heater_entity},
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to switch heater %s", self.heater_entity)
            return
        self.heater_on = on
        if ts is not None:
            self.controller.notify_changed(ts)

    # --------------------------------------------------- ready-by / scheduling
    def _lead_time_s(self, target: float) -> float:
        if self.current_temp is None:
            return READY_UNKNOWN_LEAD_S
        hours = self.model.time_to_reach(self.current_temp, target, self._last_outdoor)
        if hours is None:
            return READY_UNKNOWN_LEAD_S
        if hours <= 0:
            return 0.0
        return min(hours * 3600 * READY_MARGIN + READY_EXTRA_S, READY_MAX_LEAD_S)
    
    def _explain_target(self, now: datetime) -> tuple[str, datetime | None]:
        """Describe where the current target comes from, and until when."""
        local_now = dt_util.as_local(now)
        entries = list(self.entry.options.get("schedule", []))

        if (
            self._manual_override_until is not None
            and self._manual_override_target is not None
            and local_now < self._manual_override_until
        ):
            return "manual_override", self._manual_override_until

        period = active_period(local_now, entries) if entries else None
        if period is not None:
            end = datetime.combine(
                local_now.date(),
                time.fromisoformat(period["end"]),
                tzinfo=local_now.tzinfo,
            )
            return "schedule", end

        if self.ready_by is not None:
            if self._ready_started:
                return "preheat", self.ready_by
            return "waiting_preheat", self.ready_start

        if self.preset == PRESET_ECO:
            return "eco", None
        return "manual", None

    def _update_schedule(self, now: datetime) -> float:
        """Select scheduled target, manual override, and preheat behavior."""
        entries = list(self.entry.options.get("schedule", []))
        local_now = dt_util.as_local(now)

        # A user/reference-thermostat override lasts until its recorded boundary.
        if self._manual_override_until is not None:
            if local_now < self._manual_override_until:
                if self._manual_override_target is not None:
                    return self._manual_override_target
            else:
                self._manual_override_until = None
                self._manual_override_target = None
                self._request_save()

        period = active_period(local_now, entries) if entries else None
        if period is not None:
            if self._schedule_ready_key is not None:
                self._clear_ready()
                self._schedule_ready_key = None
            return float(period["temperature"])

        # No schedule: preserve the ordinary manual target / eco preset.
        if not entries:
            if self._schedule_ready_key is not None:
                self._clear_ready()
                self._schedule_ready_key = None
            base = self.eco_temp if self.preset == PRESET_ECO else self.target
        else:
            # Outside comfort periods, manual/reference targets remain active
            # until the next comfort period. In the absence of an override,
            # use eco while preheating for the next period.
            base = self.eco_temp
            upcoming = next_period(local_now, entries)
            if upcoming is not None:
                start, entry = upcoming
                key = (
                    start.date().isoformat(),
                    start.strftime("%H:%M:%S"),
                    tuple(entry["days"]),
                    float(entry["temperature"]),
                )
                if key != self._schedule_ready_key:
                    self.ready_by = dt_util.as_utc(start)
                    self.ready_target = float(entry["temperature"])
                    self.ready_start = None
                    self._ready_started = False
                    self._schedule_ready_key = key

        if self.ready_by is None:
            return base

        goal = self.ready_target if self.ready_target is not None else self.target
        if now >= self.ready_by:
            self.target = goal
            self.preset = PRESET_NONE
            self._clear_ready()
            self._schedule_ready_key = None
            self._request_save()
            return goal

        if not self._ready_started:
            lead = self._lead_time_s(goal)
            self.ready_start = self.ready_by - timedelta(seconds=lead)
            if now >= self.ready_start:
                self._ready_started = True

        return goal if self._ready_started else base

    def _clear_ready(self) -> None:
        self.ready_by = None
        self.ready_target = None
        self.ready_start = None
        self._ready_started = False

    # ------------------------------------------------------------- user input
    async def async_set_target(self, temp: float) -> None:
        self.target = min(max(float(temp), self.min_temp), self.max_temp)
        self.preset = PRESET_NONE

        entries = list(self.entry.options.get("schedule", []))
        local_now = dt_util.as_local(dt_util.utcnow())
        period = active_period(local_now, entries) if entries else None

        if period is not None:
            # Override the active comfort period only until its end.
            end_time = time.fromisoformat(period["end"])
            self._manual_override_until = datetime.combine(
                local_now.date(), end_time, tzinfo=local_now.tzinfo
            )
            self._manual_override_target = self.target
        elif entries:
            # Outside comfort hours, keep the new target until the next
            # comfort period begins. At that point the schedule takes over.
            upcoming = next_period(local_now, entries)
            if upcoming is not None:
                self._manual_override_until = upcoming[0]
                self._manual_override_target = self.target
            else:
                self._manual_override_until = None
                self._manual_override_target = None
        else:
            self._manual_override_until = None
            self._manual_override_target = None

        await self._after_user_change()

    async def async_set_mode(self, mode: HVACMode) -> None:
        self.hvac_mode = mode
        if mode == HVACMode.OFF:
            self.controller.reset_tracking()
            await self._set_heater(False, dt_util.utcnow().timestamp())
        await self._after_user_change()

    async def async_set_preset(self, preset: str) -> None:
        self.preset = preset
        await self._after_user_change()

    async def async_set_ready_by(self, at: datetime, temp: float | None) -> None:
        if at <= dt_util.utcnow():
            raise ServiceValidationError("The ready-by time must be in the future")
        if temp is not None:
            temp = min(max(temp, self.min_temp), self.max_temp)
        self.ready_by, self.ready_target = at, temp
        self.ready_start = None
        self._ready_started = False
        await self._after_user_change()

    async def async_clear_ready_by(self) -> None:
        self._clear_ready()
        await self._after_user_change()

    async def async_reset_learning(self) -> None:
        self.learner.reset()
        self.controller.coast = 0.2
        await self._after_user_change()

    async def _after_user_change(self) -> None:
        self._request_save()
        await self._async_tick()

    # ------------------------------------------------------ values for sensors
    @property
    def model(self):
        return self.learner.model

    @property
    def heating_rate(self) -> float | None:
        """Temperature rise [°C/h] with the heater on, at current conditions."""
        if self.current_temp is None:
            return None
        return self.model.rate(self.current_temp, self._last_outdoor, 1.0)

    @property
    def cooling_rate(self) -> float | None:
        """Temperature loss [°C/h] with the heater off, at current conditions."""
        if self.current_temp is None:
            return None
        return -self.model.rate(self.current_temp, self._last_outdoor, 0.0)

    @property
    def minutes_to_target(self) -> float | None:
        if self.current_temp is None:
            return None
        hours = self.model.time_to_reach(
            self.current_temp, self.effective_target, self._last_outdoor
        )
        return None if hours is None else round(hours * 60, 1)

    @property
    def max_reachable(self) -> float:
        return self.model.equilibrium(self._last_outdoor, 1.0)

    @property
    def learning_progress(self) -> float:
        return round(self.learner.model.progress * 100)

    @property
    def coast(self) -> float:
        return self.controller.coast

    @property
    def heater_lag_min(self) -> float:
        return self.learner.heater_lag_s / 60
    
    @property
    def status(self) -> str:
        if self.hvac_mode == HVACMode.OFF:
            return "off"
        if self.current_temp is None:
            return "sensor_unavailable"
        if self.window_open:
            return "window_open"
        if self.heater_on:
            return "preheating" if self.target_source == "preheat" else "heating"
        if self._holding:
            return "waiting_min_time"
        return "idle"
