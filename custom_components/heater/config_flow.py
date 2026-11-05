"""Config + options flow for Heat Learner."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from datetime import time

from homeassistant.helpers import selector
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
    """Configure Heat Learner settings and weekly schedule."""

    def async_step_init(self, user_input=None):
        """Show the configuration menu."""
        if not hasattr(self, "_options"):
            self._options = dict(self.config_entry.options)

        menu_options = ["settings", "add_period"]
        if self._options.get("schedule"):
            menu_options.append("remove_period")
        menu_options.append("finish")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_settings(self, user_input=None):
        """Edit existing thermostat settings."""
        cur = {**self.config_entry.data, **self._options}

        def default(key, fallback):
            return cur.get(key, fallback)

        schema = vol.Schema({
            vol.Optional(
                CONF_HYSTERESIS,
                default=default(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
            ): _number(0.1, 2.0, 0.1, "°C"),
            vol.Optional(
                CONF_MIN_ON,
                default=default(CONF_MIN_ON, DEFAULT_MIN_ON),
            ): _number(0, 60, 1, "min"),
            vol.Optional(
                CONF_MIN_OFF,
                default=default(CONF_MIN_OFF, DEFAULT_MIN_OFF),
            ): _number(0, 60, 1, "min"),
            vol.Optional(
                CONF_ECO_TEMP,
                default=default(CONF_ECO_TEMP, DEFAULT_ECO_TEMP),
            ): _number(5, 25, 0.5, "°C"),
            vol.Optional(
                CONF_MIN_TEMP,
                default=default(CONF_MIN_TEMP, DEFAULT_MIN_TEMP),
            ): _number(0, 25, 0.5, "°C"),
            vol.Optional(
                CONF_MAX_TEMP,
                default=default(CONF_MAX_TEMP, DEFAULT_MAX_TEMP),
            ): _number(15, 35, 0.5, "°C"),
        })

        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_init()

        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
        )

    async def async_step_add_period(self, user_input=None):
        """Add one or more weekday periods."""
        days = [
            {"label": label, "value": str(number)}
            for number, label in enumerate([
                "Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday",
            ])
        ]

        schema = vol.Schema({
            vol.Required("days"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=days,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required("start"): selector.TimeSelector(),
            vol.Required("end"): selector.TimeSelector(),
            vol.Required("temperature", default=20.0): _number(
                5, 35, 0.5, "°C"
            ),
        })

        if user_input is not None:
            start = time.fromisoformat(str(user_input["start"]))
            end = time.fromisoformat(str(user_input["end"]))

            if not user_input["days"]:
                return self.async_show_form(
                    step_id="add_period",
                    data_schema=schema,
                    errors={"days": "required"},
                )

            if end <= start:
                return self.async_show_form(
                    step_id="add_period",
                    data_schema=schema,
                    errors={"end": "invalid_period"},
                )

            period = {
                "days": sorted({int(day) for day in user_input["days"]}),
                "start": start.strftime("%H:%M:%S"),
                "end": end.strftime("%H:%M:%S"),
                "temperature": float(user_input["temperature"]),
            }

            schedule = list(self._options.get("schedule", []))

            # Reject overlapping periods on any shared weekday.
            for existing in schedule:
                if not set(period["days"]) & set(existing["days"]):
                    continue

                overlaps = (
                    start < time.fromisoformat(existing["end"])
                    and time.fromisoformat(existing["start"]) < end
                )
                if overlaps:
                    return self.async_show_form(
                        step_id="add_period",
                        data_schema=schema,
                        errors={"base": "overlap"},
                    )

            schedule.append(period)
            self._options["schedule"] = schedule
            return await self.async_step_init()

        return self.async_show_form(
            step_id="add_period",
            data_schema=schema,
        )

    async def async_step_remove_period(self, user_input=None):
        """Remove a previously configured period."""
        schedule = list(self._options.get("schedule", []))

        if not schedule:
            return await self.async_step_init()

        if user_input is not None:
            index = int(user_input["period"])
            if 0 <= index < len(schedule):
                schedule.pop(index)
            self._options["schedule"] = schedule
            return await self.async_step_init()

        options = []
        for index, entry in enumerate(schedule):
            day_names = [
                ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day]
                for day in entry["days"]
            ]
            label = (
                f"{', '.join(day_names)} "
                f"{entry['start'][:5]}–{entry['end'][:5]} "
                f"({entry['temperature']} °C)"
            )
            options.append({"label": label, "value": str(index)})

        schema = vol.Schema({
            vol.Required("period"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        })

        return self.async_show_form(
            step_id="remove_period",
            data_schema=schema,
        )

    async def async_step_finish(self, user_input=None):
        """Save options and reload the integration."""
        return self.async_create_entry(
            title="",
            data=self._options,
        )

