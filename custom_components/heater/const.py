"""Constants for Heat Learner."""
from __future__ import annotations

DOMAIN = "heater"

CONF_TEMP_SENSOR = "temperature_sensor"
CONF_HEATER = "heater_switch"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_WINDOW_SENSOR = "window_sensor"
CONF_REFERENCE_CLIMATE = "reference_climate"

CONF_HYSTERESIS = "hysteresis"
CONF_MIN_ON = "min_on_minutes"
CONF_MIN_OFF = "min_off_minutes"
CONF_ECO_TEMP = "eco_temperature"
CONF_MIN_TEMP = "min_temperature"
CONF_MAX_TEMP = "max_temperature"

DEFAULT_NAME = "Heater"
DEFAULT_HYSTERESIS = 0.3
DEFAULT_MIN_ON = 3
DEFAULT_MIN_OFF = 3
DEFAULT_ECO_TEMP = 17.0
DEFAULT_MIN_TEMP = 7.0
DEFAULT_MAX_TEMP = 28.0
DEFAULT_TARGET = 20.0

TICK_SECONDS = 30
STARTUP_GRACE_S = 120  # don't fail-safe on missing sensors right after start
READY_MARGIN = 1.15  # safety factor on the learned heat-up time
READY_EXTRA_S = 300
READY_UNKNOWN_LEAD_S = 3 * 3600  # heat-up time unknown -> start 3 h early
READY_MAX_LEAD_S = 12 * 3600

SERVICE_SET_READY_BY = "set_ready_by"
SERVICE_CLEAR_READY_BY = "clear_ready_by"
SERVICE_RESET_LEARNING = "reset_learning"

ATTR_TIME = "time"
ATTR_TEMPERATURE_TARGET = "temperature"

STORAGE_VERSION = 1
SAVE_DELAY_S = 60
