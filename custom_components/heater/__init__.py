"""Heat Learner: a self-learning on/off thermostat for electric heaters."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .manager import HeatLearnerManager

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR]

type HeatLearnerConfigEntry = ConfigEntry[HeatLearnerManager]


async def async_setup_entry(hass: HomeAssistant, entry: HeatLearnerConfigEntry) -> bool:
    manager = HeatLearnerManager(hass, entry)
    await manager.async_start()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HeatLearnerConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return unload_ok


async def _async_reload(hass: HomeAssistant, entry: HeatLearnerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
