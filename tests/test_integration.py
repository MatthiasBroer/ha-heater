"""Integration test inside a real Home Assistant core instance."""
from datetime import timedelta

from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.heat_learner.const import (
    CONF_HEATER,
    CONF_OUTDOOR_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_WINDOW_SENSOR,
    DOMAIN,
)


async def _setup(hass: HomeAssistant):
    hass.states.async_set("sensor.room", "18.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.out", "4.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("switch.heater", "off")
    hass.states.async_set("binary_sensor.window", "off")
    on = async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living",
        data={
            "name": "Living",
            CONF_TEMP_SENSOR: "sensor.room",
            CONF_HEATER: "switch.heater",
            CONF_OUTDOOR_SENSOR: "sensor.out",
            CONF_WINDOW_SENSOR: "binary_sensor.window",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, on, off


async def _tick(hass, seconds=30):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_heats_and_stops(hass: HomeAssistant):
    entry, on, off = await _setup(hass)
    assert hass.states.get("climate.living") is not None
    assert hass.states.get("sensor.living_time_to_reach_target") is not None

    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": "climate.living", "temperature": 21}, blocking=True)
    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": "climate.living", "hvac_mode": HVACMode.HEAT}, blocking=True)
    await hass.async_block_till_done()
    assert len(on) == 1, "cold room -> heater on"

    hass.states.async_set("switch.heater", "on")
    hass.states.async_set("sensor.room", "21.4", {"unit_of_measurement": "°C"})
    # pretend the heater has already been on for longer than the 3 min minimum
    entry.runtime_data.controller.last_change_ts -= 400
    await _tick(hass)
    assert len(off) >= 1, "above target -> heater off"
    assert hass.states.get("climate.living").attributes["effective_target"] == 21


async def test_window_open_turns_off(hass: HomeAssistant):
    entry, on, off = await _setup(hass)
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": "climate.living", "temperature": 21}, blocking=True)
    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": "climate.living", "hvac_mode": HVACMode.HEAT}, blocking=True)
    hass.states.async_set("switch.heater", "on")
    hass.states.async_set("binary_sensor.window", "on")
    off.clear()
    await _tick(hass)
    assert len(off) == 1


async def test_ready_by_service_and_sensors(hass: HomeAssistant):
    entry, on, off = await _setup(hass)
    await hass.services.async_call(
        "heater", "set_ready_by",
        {"entity_id": "climate.living", "time": "07:00:00", "temperature": 21},
        blocking=True)
    await hass.async_block_till_done()
    attrs = hass.states.get("climate.living").attributes
    assert attrs["ready_by"] is not None
    assert attrs["ready_temperature"] == 21
    await hass.services.async_call(
        "heater", "clear_ready_by", {"entity_id": "climate.living"}, blocking=True)
    assert hass.states.get("climate.living").attributes["ready_by"] is None


async def test_unload_turns_heater_off(hass: HomeAssistant):
    entry, on, off = await _setup(hass)
    await hass.services.async_call(
        "climate", "set_hvac_mode",
        {"entity_id": "climate.living", "hvac_mode": HVACMode.HEAT}, blocking=True)
    off.clear()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(off) == 1
