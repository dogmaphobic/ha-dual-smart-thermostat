"""Behavioral tests for diagnostic binary sensors."""

from datetime import timedelta

from homeassistant.components.climate import HVACMode
from homeassistant.components.climate.const import DOMAIN as CLIMATE
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.dual_smart_thermostat.const import DOMAIN

from . import common, setup_sensor, setup_switch


async def test_actuator_problem_binary_sensor_turns_on_when_command_times_out(
    hass: HomeAssistant,
) -> None:
    """Flag actuator problems when a command is issued but the entity never changes."""
    hass.config.units = METRIC_SYSTEM
    setup_sensor(hass, 18.0)
    setup_switch(hass, False)

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                "target_temp": 20,
                "cold_tolerance": 0.3,
                "hot_tolerance": 0.3,
                "initial_hvac_mode": HVACMode.HEAT,
                "actuator_state_timeout": timedelta(seconds=30),
            }
        },
    )
    await hass.async_block_till_done()

    actuator_problem = hass.states.get("binary_sensor.test_actuator_problem")
    assert actuator_problem is not None
    assert actuator_problem.state == STATE_OFF

    common.async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=31),
        fire_all=True,
    )
    await hass.async_block_till_done()

    actuator_problem = hass.states.get("binary_sensor.test_actuator_problem")
    assert actuator_problem is not None
    assert actuator_problem.state == STATE_ON
    assert common.ENT_SWITCH in actuator_problem.attributes["faulty_entities"]
    assert (
        actuator_problem.attributes["details"][common.ENT_SWITCH]["reason"]
        == "command_timeout"
    )


async def test_actuator_problem_binary_sensor_turns_on_when_entity_unavailable(
    hass: HomeAssistant,
) -> None:
    """Flag actuator problems when the controlled entity disappears."""
    hass.config.units = METRIC_SYSTEM
    setup_sensor(hass, 22.0)
    setup_switch(hass, False)

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                "target_temp": 18,
                "cold_tolerance": 0.3,
                "hot_tolerance": 0.3,
                "initial_hvac_mode": HVACMode.HEAT,
                "actuator_state_timeout": timedelta(seconds=30),
            }
        },
    )
    await hass.async_block_till_done()

    hass.states.async_set(common.ENT_SWITCH, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    actuator_problem = hass.states.get("binary_sensor.test_actuator_problem")
    assert actuator_problem is not None
    assert actuator_problem.state == STATE_ON
    assert (
        actuator_problem.attributes["details"][common.ENT_SWITCH]["reason"]
        == "entity_unavailable"
    )


async def test_temperature_progress_binary_sensor_flags_and_recovers(
    hass: HomeAssistant,
) -> None:
    """Flag insufficient temperature progress during active heating and clear on recovery."""
    hass.config.units = METRIC_SYSTEM
    setup_sensor(hass, 18.0)
    setup_switch(hass, True)

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                "target_temp": 20,
                "cold_tolerance": 0.3,
                "hot_tolerance": 0.3,
                "initial_hvac_mode": HVACMode.HEAT,
                "temperature_change_threshold": 1.0,
                "temperature_change_duration": timedelta(minutes=30),
            }
        },
    )
    await hass.async_block_till_done()

    temp_problem = hass.states.get("binary_sensor.test_temperature_progress_problem")
    assert temp_problem is not None
    assert temp_problem.state == STATE_OFF

    common.async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(minutes=31),
        fire_all=True,
    )
    await hass.async_block_till_done()

    temp_problem = hass.states.get("binary_sensor.test_temperature_progress_problem")
    assert temp_problem is not None
    assert temp_problem.state == STATE_ON

    hass.states.async_set(common.ENT_SENSOR, 19.2)
    await hass.async_block_till_done()

    temp_problem = hass.states.get("binary_sensor.test_temperature_progress_problem")
    assert temp_problem is not None
    assert temp_problem.state == STATE_OFF
