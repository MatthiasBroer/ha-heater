"""Sensors exposing what Heat Learner has learned about the room."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HeatLearnerConfigEntry
from .const import DOMAIN
from .manager import HeatLearnerManager


@dataclass(frozen=True, kw_only=True)
class HeatLearnerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[HeatLearnerManager], float | None]


SENSORS: tuple[HeatLearnerSensorDescription, ...] = (
    HeatLearnerSensorDescription(
        key="heating_rate",
        translation_key="heating_rate",
        native_unit_of_measurement="°C/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:thermometer-chevron-up",
        value_fn=lambda m: m.heating_rate,
    ),
    HeatLearnerSensorDescription(
        key="cooling_rate",
        translation_key="cooling_rate",
        native_unit_of_measurement="°C/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:thermometer-chevron-down",
        value_fn=lambda m: m.cooling_rate,
    ),
    HeatLearnerSensorDescription(
        key="time_to_target",
        translation_key="time_to_target",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda m: m.minutes_to_target,
    ),
    HeatLearnerSensorDescription(
        key="max_reachable",
        translation_key="max_reachable",
        native_unit_of_measurement="°C",
        suggested_display_precision=1,
        icon="mdi:thermometer-high",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: round(m.max_reachable, 1),
    ),
    HeatLearnerSensorDescription(
        key="overshoot",
        translation_key="overshoot",
        native_unit_of_measurement="°C",
        suggested_display_precision=2,
        icon="mdi:thermometer-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.coast,
    ),
    HeatLearnerSensorDescription(
        key="heater_lag",
        translation_key="heater_lag",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.heater_lag_min,
    ),
    HeatLearnerSensorDescription(
        key="learning_progress",
        translation_key="learning_progress",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        icon="mdi:school-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.learning_progress,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HeatLearnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        HeatLearnerSensor(entry.runtime_data, entry, desc) for desc in SENSORS
    )


class HeatLearnerSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: HeatLearnerSensorDescription

    def __init__(
        self,
        manager: HeatLearnerManager,
        entry: HeatLearnerConfigEntry,
        description: HeatLearnerSensorDescription,
    ) -> None:
        self._m = manager
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
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

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self._m)
