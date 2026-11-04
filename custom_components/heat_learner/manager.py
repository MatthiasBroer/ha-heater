"""Runtime brain of a Heat Learner config entry.

Reads the sensors every TICK_SECONDS, feeds the learner, runs the on/off
controller, switches the heater and tells the entities to refresh.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
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
from homeassistant.helpers.event import async_track_time_interval
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

        # user-facing state (persisted)
        self.hvac_mode: HVACMode = HVACMode.OFF
        self.target: float = DEFAULT_TARGET
        self.preset: str = PRESET_NONE
        self.ready_by: datetime | None = None
        self.ready_target: float | None = None

        # live values
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

    # ------------------------------------------------------------------ setup
    async def async_start(self) -> None:
        data = await self._store.async_load()
        if data:
            self._restore(data)
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

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        await self._store.async_save(self._data_to_save())
        if self.hvac_mode == HVACMode.HEAT:
            await self._set_heater(False)  # fail-safe: never leave a heater unsupervised

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
        }

    def _restore(self, data: dict[str, Any]) -> None:
        try:
            self.hvac_mode = HVACMode(data.get("hvac_mode", HVACMode.OFF))
            self.target = float(data.get("target", DEFAULT_TARGET))
            self.preset = data.get("preset", PRESET_NONE)
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

    # ----------------------------------------------------------------- inputs
    def _temp_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
            except Exception:  # noqa: BLE001 - unknown unit
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

    # ---------------------------------------------------------------- control
    async def _async_tick(self, now: datetime | None = None) -> None:
        now = now or dt_util.utcnow()
        ts = now.timestamp()
        self._read_inputs()
        temp = self.current_temp
        self.sensor_ok = temp is not None

        # 1) learn (heater state read *before* any action = state of last interval)
        if self.learner.tick(
            ts, temp, self.heater_on, self.outdoor_temp, valid=not self.window_open
        ):
            self._request_save()

        # 2) schedule / target
        self.effective_target = self._update_schedule(now)

        # 3) control
        if self.hvac_mode == HVACMode.HEAT:
            await self._async_control(ts, temp)
        else:
            self.controller.reset_tracking()

        async_dispatcher_send(self.hass, self.signal)

    async def _async_control(self, ts: float, temp: float | None) -> None:
        if temp is None:
            # fail-safe: no temperature -> no heating (after startup grace)
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
        hours = self.model.time_to_reach(
            self.current_temp, target, self._last_outdoor
        )
        if hours is None:
            return READY_UNKNOWN_LEAD_S
        if hours <= 0:
            return 0.0
        return min(hours * 3600 * READY_MARGIN + READY_EXTRA_S, READY_MAX_LEAD_S)

    def _update_schedule(self, now: datetime) -> float:
        base = self.eco_temp if self.preset == PRESET_ECO else self.target
        if self.ready_by is None:
            return base
        goal = self.ready_target if self.ready_target is not None else self.target
        if now >= self.ready_by:
            # arrived: the ready-by temperature becomes the new comfort target
            self.target, self.preset = goal, PRESET_NONE
            self._clear_ready()
            self._request_save()
            return goal
        if not self._ready_started:
            lead = self._lead_time_s(goal)
            self.ready_start = self.ready_by - timedelta(seconds=lead)
            if now >= self.ready_start:
                self._ready_started = True  # latch: no flapping once preheating began
        return goal if self._ready_started else base

    def _clear_ready(self) -> None:
        self.ready_by = None
        self.ready_target = None
        self.ready_start = None
        self._ready_started = False

    # ------------------------------------------------------------- user input
    async def async_set_target(self, temp: float) -> None:
        self.target = min(max(temp, self.min_temp), self.max_temp)
        self.preset = PRESET_NONE
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
