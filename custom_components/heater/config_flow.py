"""Config + options flow for Heat Learner."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import HeatLearnerConfigEntry
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
    DEFAULT_NAME,
    DOMAIN,
)


def _number(minimum: float, maximum: float, step: float, unit: str | None = None):
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class HeatLearnerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HEATER])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                ),
                vol.Required(CONF_HEATER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["switch", "input_boolean"])
                ),
                vol.Optional(CONF_OUTDOOR_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                ),
                vol.Optional(CONF_WINDOW_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: HeatLearnerConfigEntry) -> OptionsFlow:
        return HeatLearnerOptionsFlow()


class HeatLearnerOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        cur = {**self.config_entry.data, **self.config_entry.options}

        def default(key: str, fallback: float) -> float:
            return cur.get(key, fallback)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_HYSTERESIS, default=default(CONF_HYSTERESIS, DEFAULT_HYSTERESIS)
                ): _number(0.1, 2.0, 0.1, "°C"),
                vol.Optional(
                    CONF_MIN_ON, default=default(CONF_MIN_ON, DEFAULT_MIN_ON)
                ): _number(0, 60, 1, "min"),
                vol.Optional(
                    CONF_MIN_OFF, default=default(CONF_MIN_OFF, DEFAULT_MIN_OFF)
                ): _number(0, 60, 1, "min"),
                vol.Optional(
                    CONF_ECO_TEMP, default=default(CONF_ECO_TEMP, DEFAULT_ECO_TEMP)
                ): _number(5, 25, 0.5, "°C"),
                vol.Optional(
                    CONF_MIN_TEMP, default=default(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)
                ): _number(0, 25, 0.5, "°C"),
                vol.Optional(
                    CONF_MAX_TEMP, default=default(CONF_MAX_TEMP, DEFAULT_MAX_TEMP)
                ): _number(15, 35, 0.5, "°C"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
