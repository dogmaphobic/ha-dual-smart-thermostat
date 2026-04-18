"""Behavioral tests for shared global HVAC mode coordination."""

from homeassistant.components.climate import HVACMode
from homeassistant.components.climate.const import DOMAIN as CLIMATE
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM

from custom_components.dual_smart_thermostat.const import (
    ATTR_SHARED_HVAC_FOLLOWING,
    ATTR_SHARED_HVAC_MODE,
    ATTR_SHARED_LAST_NON_OFF_HVAC_MODE,
    DOMAIN,
)

from . import common, setup_sensor


def _setup_shared_hvac_mode_helper(
    hass: HomeAssistant, initial_mode: HVACMode = HVACMode.HEAT
) -> None:
    """Create a simple input_select-like helper for shared HVAC mode tests."""
    helper_entity_id = "input_select.shared_hvac_mode"
    helper_attrs = {"options": [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]}
    hass.states.async_set(helper_entity_id, initial_mode, helper_attrs)

    @callback
    def select_option(call) -> None:
        hass.states.async_set(helper_entity_id, call.data["option"], helper_attrs)

    hass.services.async_register("input_select", "select_option", select_option)


def _setup_test_switches(hass: HomeAssistant, *entity_ids: str) -> None:
    """Register dummy switch entities plus the generic HA turn_on/turn_off services."""
    for entity_id in entity_ids:
        hass.states.async_set(entity_id, STATE_OFF)

    common.async_mock_service(hass, "homeassistant", SERVICE_TURN_ON)
    common.async_mock_service(hass, "homeassistant", SERVICE_TURN_OFF)


async def test_shared_hvac_mode_preserves_locally_off_zones(
    hass: HomeAssistant,
) -> None:
    """A zone turned off locally must stay off while the shared helper changes."""
    hass.config.units = METRIC_SYSTEM
    setup_sensor(hass, 22.0)
    _setup_shared_hvac_mode_helper(hass, HVACMode.HEAT)
    _setup_test_switches(
        hass,
        "switch.zone_1_heat",
        "switch.zone_1_cool",
        "switch.zone_2_heat",
        "switch.zone_2_cool",
    )

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": [
                {
                    "platform": DOMAIN,
                    "name": "Zone One",
                    "heater": "switch.zone_1_heat",
                    "cooler": "switch.zone_1_cool",
                    "target_sensor": common.ENT_SENSOR,
                    "target_temp": 21,
                    "cold_tolerance": 0.3,
                    "hot_tolerance": 0.3,
                    "initial_hvac_mode": HVACMode.HEAT,
                    "shared_hvac_mode_entity": "input_select.shared_hvac_mode",
                },
                {
                    "platform": DOMAIN,
                    "name": "Zone Two",
                    "heater": "switch.zone_2_heat",
                    "cooler": "switch.zone_2_cool",
                    "target_sensor": common.ENT_SENSOR,
                    "target_temp": 21,
                    "cold_tolerance": 0.3,
                    "hot_tolerance": 0.3,
                    "initial_hvac_mode": HVACMode.HEAT,
                    "shared_hvac_mode_entity": "input_select.shared_hvac_mode",
                },
            ]
        },
    )
    await hass.async_block_till_done()

    assert hass.states.get("climate.zone_one").state == HVACMode.HEAT
    assert hass.states.get("climate.zone_two").state == HVACMode.HEAT

    await common.async_set_hvac_mode(hass, HVACMode.OFF, entity_id="climate.zone_one")
    await hass.async_block_till_done()

    zone_one = hass.states.get("climate.zone_one")
    assert zone_one is not None
    assert zone_one.state == HVACMode.OFF
    assert zone_one.attributes[ATTR_SHARED_HVAC_FOLLOWING] is False

    await common.async_set_hvac_mode(hass, HVACMode.COOL, entity_id="climate.zone_two")
    await hass.async_block_till_done()

    zone_one = hass.states.get("climate.zone_one")
    zone_two = hass.states.get("climate.zone_two")
    assert zone_one is not None
    assert zone_two is not None
    assert hass.states.get("input_select.shared_hvac_mode").state == HVACMode.COOL
    assert zone_one.state == HVACMode.OFF
    assert zone_two.state == HVACMode.COOL

    await common.async_turn_on(hass, entity_id="climate.zone_one")
    await hass.async_block_till_done()

    zone_one = hass.states.get("climate.zone_one")
    assert zone_one is not None
    assert zone_one.state == HVACMode.COOL
    assert zone_one.attributes[ATTR_SHARED_HVAC_FOLLOWING] is True


async def test_shared_hvac_mode_restore_keeps_participating_zone_off_until_global_mode_returns(
    hass: HomeAssistant,
) -> None:
    """Zones restore their local participation state separately from shared off."""
    hass.config.units = METRIC_SYSTEM
    setup_sensor(hass, 22.0)
    _setup_shared_hvac_mode_helper(hass, HVACMode.OFF)
    _setup_test_switches(
        hass,
        "switch.zone_1_heat",
        "switch.zone_1_cool",
    )

    common.mock_restore_cache(
        hass,
        [
            State(
                "climate.zone_one",
                HVACMode.OFF,
                {
                    ATTR_SHARED_HVAC_FOLLOWING: True,
                    ATTR_SHARED_HVAC_MODE: HVACMode.OFF,
                    ATTR_SHARED_LAST_NON_OFF_HVAC_MODE: HVACMode.HEAT,
                },
            )
        ],
    )

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "Zone One",
                "heater": "switch.zone_1_heat",
                "cooler": "switch.zone_1_cool",
                "target_sensor": common.ENT_SENSOR,
                "target_temp": 21,
                "cold_tolerance": 0.3,
                "hot_tolerance": 0.3,
                "initial_hvac_mode": HVACMode.HEAT,
                "shared_hvac_mode_entity": "input_select.shared_hvac_mode",
            }
        },
    )
    await hass.async_block_till_done()

    zone_one = hass.states.get("climate.zone_one")
    assert zone_one is not None
    assert zone_one.state == HVACMode.OFF
    assert zone_one.attributes[ATTR_SHARED_HVAC_FOLLOWING] is True
    assert zone_one.attributes[ATTR_SHARED_LAST_NON_OFF_HVAC_MODE] == HVACMode.HEAT

    await common.async_turn_on(hass, entity_id="climate.zone_one")
    await hass.async_block_till_done()

    zone_one = hass.states.get("climate.zone_one")
    assert zone_one is not None
    assert hass.states.get("input_select.shared_hvac_mode").state == HVACMode.HEAT
    assert zone_one.state == HVACMode.HEAT
