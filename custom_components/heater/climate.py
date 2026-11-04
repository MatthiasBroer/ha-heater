"""Climate entity: the thermostat you use in the UI."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HeatLearnerConfigEntry
from .const import (
    ATTR_TEMPERATURE_TARGET,
    ATTR_TIME,
    DOMAIN,
    SERVICE_CLEAR_READY_BY,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_READY_BY,
)
from .manager import HeatLearnerManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeatLearnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([HeatLearnerClimate(entry.runtime_data, entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_READY_BY,
        {
            vol.Required(ATTR_TIME): cv.time,
            vol.Optional(ATTR_TEMPERATURE_TARGET): vol.Coerce(float),
        },
        "async_service_set_ready_by",
    )
    platform.async_register_entity_service(
        SERVICE_CLEAR_READY_BY, None, "async_service_clear_ready_by"
    )
    platform.async_register_entity_service(
        SERVICE_RESET_LEARNING, None, "async_service_reset_learning"
    )


class HeatLearnerClimate(ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_preset_modes = [PRESET_NONE, PRESET_ECO]
    _attr_target_temperature_step = 0.5
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, manager: HeatLearnerManager, entry: HeatLearnerConfigEntry) -> None:
        self._m = manager
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    async def async_added_to_hass(self) -> None:
        @callback
        def _refresh() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._m.signal, _refresh)
        )

    # ---- state
    @property
    def available(self) -> bool:
        return self._m.sensor_ok

    @property
    def current_temperature(self) -> float | None:
        return self._m.current_temp

    @property
    def target_temperature(self) -> float:
        return self._m.target

    @property
    def min_temp(self) -> float:
        return self._m.min_temp

    @property
    def max_temp(self) -> float:
        return self._m.max_temp

    @property
    def hvac_mode(self) -> HVACMode:
        return self._m.hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        if self._m.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if self._m.heater_on else HVACAction.IDLE

    @property
    def preset_mode(self) -> str:
        return self._m.preset

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._m
        return {
            "effective_target": m.effective_target,
            "window_open": m.window_open,
            "ready_by": m.ready_by.isoformat() if m.ready_by else None,
            "ready_temperature": m.ready_target,
            "preheat_starts_at": m.ready_start.isoformat() if m.ready_start else None,
            "preheating": m._ready_started,
            "minutes_to_target": m.minutes_to_target,
            "learning_progress": m.learning_progress,
            "learned_coast": round(m.coast, 2),
            "learned_heater_lag_min": m.heater_lag_min,
        }

    # ---- commands
    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self._m.async_set_target(float(temp))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._m.async_set_mode(hvac_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._m.async_set_preset(preset_mode)

    # ---- entity services
    async def async_service_set_ready_by(
        self, time: Any, temperature: float | None = None
    ) -> None:
        """Be at the target temperature at the next occurrence of ``time``."""
        now = dt_util.now()
        at = now.replace(
            hour=time.hour, minute=time.minute, second=time.second, microsecond=0
        )
        if at <= now:
            at += timedelta(days=1)
        await self._m.async_set_ready_by(dt_util.as_utc(at), temperature)

    async def async_service_clear_ready_by(self) -> None:
        await self._m.async_clear_ready_by()

    async def async_service_reset_learning(self) -> None:
        await self._m.async_reset_learning()
