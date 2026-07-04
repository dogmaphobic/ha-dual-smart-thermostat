"""Diagnostic binary sensors for Dual Smart Thermostat.

These sensors intentionally model live fault conditions instead of latched alarms:

- actuator problem: the controlled entity is unavailable or does not reach the
  requested state within the configured timeout
- temperature progress problem: while actively heating or cooling, the measured
  temperature does not move enough in the expected direction within the
  configured observation window

Because they represent live conditions, both sensors auto-clear when the
underlying problem clears. Users who want acknowledgement, latching, or manual
reset semantics should implement that policy in automations/AppDaemon rather
than inside the thermostat entity itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.climate import HVACAction
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    CONF_UNIQUE_ID,
    EntityCategory,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_ACTUATOR_STATE_TIMEOUT,
    CONF_AUX_COOLER,
    CONF_AUX_HEATER,
    CONF_COOLER,
    CONF_HEATER,
    CONF_SENSOR,
    CONF_TEMPERATURE_CHANGE_DURATION,
    CONF_TEMPERATURE_CHANGE_THRESHOLD,
    DEFAULT_ACTUATOR_STATE_TIMEOUT,
    DOMAIN,
    SIGNAL_ACTUATOR_DIAGNOSTICS,
    SIGNAL_CLIMATE_DIAGNOSTICS,
    build_runtime_key,
)

_UNAVAILABLE_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN}


def _normalize_config_values(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize duration values loaded from config entries or YAML discovery."""
    normalized = dict(config)
    duration_keys = [
        CONF_ACTUATOR_STATE_TIMEOUT,
        CONF_TEMPERATURE_CHANGE_DURATION,
    ]

    for key in duration_keys:
        value = normalized.get(key)
        if value is None or isinstance(value, timedelta):
            continue
        if isinstance(value, (int, float)):
            normalized[key] = timedelta(seconds=value)
        elif isinstance(value, dict) and any(k in value for k in ("hours", "minutes")):
            normalized[key] = timedelta(
                hours=value.get("hours", 0),
                minutes=value.get("minutes", 0),
                seconds=value.get("seconds", 0),
            )
        elif isinstance(value, dict) and all(
            k in value for k in ("days", "seconds", "microseconds")
        ):
            normalized[key] = timedelta(
                days=value["days"],
                seconds=value["seconds"],
                microseconds=value["microseconds"],
            )

    return normalized


def _get_monitored_actuator_ids(config: dict[str, Any]) -> list[str]:
    """Return the actuator entity IDs relevant for heating and cooling."""
    entity_ids: list[str] = []
    for key in (CONF_HEATER, CONF_COOLER, CONF_AUX_HEATER, CONF_AUX_COOLER):
        entity_id = config.get(key)
        if entity_id and entity_id not in entity_ids:
            entity_ids.append(entity_id)
    return entity_ids


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic binary sensors from a config entry."""
    config = _normalize_config_values({**config_entry.data, **config_entry.options})
    runtime_key = build_runtime_key(config[CONF_NAME], config_entry.entry_id)
    unique_id_base = config_entry.entry_id
    await _async_setup_entities(
        hass,
        config,
        runtime_key,
        unique_id_base,
        async_add_entities,
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up diagnostic binary sensors for YAML-configured thermostats."""
    if discovery_info and isinstance(discovery_info, dict) and "config" in discovery_info:
        config = discovery_info["config"]

    normalized = _normalize_config_values(dict(config))
    runtime_key = build_runtime_key(
        normalized[CONF_NAME],
        normalized.get(CONF_UNIQUE_ID),
    )
    unique_id_base = normalized.get(CONF_UNIQUE_ID)
    await _async_setup_entities(
        hass,
        normalized,
        runtime_key,
        unique_id_base,
        async_add_entities,
    )


async def _async_setup_entities(
    hass: HomeAssistant,
    config: dict[str, Any],
    runtime_key: str,
    unique_id_base: str | None,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create diagnostic entities for a thermostat config.

    Every thermostat with at least one controllable actuator gets an actuator
    problem sensor. The temperature progress sensor is opt-in because it needs
    site-specific tuning to avoid false positives.
    """
    actuator_ids = _get_monitored_actuator_ids(config)
    if not actuator_ids:
        return

    entities: list[BinarySensorEntity] = [
        ActuatorFaultBinarySensor(
            hass,
            config[CONF_NAME],
            runtime_key,
            actuator_ids,
            config.get(
                CONF_ACTUATOR_STATE_TIMEOUT,
                timedelta(seconds=DEFAULT_ACTUATOR_STATE_TIMEOUT),
            ),
            unique_id_base,
        )
    ]

    temp_threshold = config.get(CONF_TEMPERATURE_CHANGE_THRESHOLD)
    temp_duration = config.get(CONF_TEMPERATURE_CHANGE_DURATION)
    if temp_threshold is not None and temp_duration is not None:
        entities.append(
            TemperatureProgressBinarySensor(
                hass,
                config[CONF_NAME],
                runtime_key,
                config[CONF_SENSOR],
                float(temp_threshold),
                temp_duration,
                unique_id_base,
            )
        )

    async_add_entities(entities)


class ActuatorFaultBinarySensor(BinarySensorEntity):
    """Binary sensor that flags actuator command or availability problems.

    The sensor tracks all actuator entities attached to a thermostat
    (heater/cooler/secondary heater). It turns on when:

    - an actuator becomes ``unknown`` or ``unavailable``
    - a command is sent and the entity never reaches the expected state within
      ``actuator_state_timeout``

    The fault clears automatically when the entity becomes available again and,
    for command timeout faults, eventually reaches the expected state.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        thermostat_name: str,
        runtime_key: str,
        actuator_ids: list[str],
        timeout: timedelta,
        unique_id_base: str | None,
    ) -> None:
        self.hass = hass
        self._runtime_key = runtime_key
        self._actuator_ids = actuator_ids
        self._timeout = timeout
        self._faults: dict[str, dict[str, Any]] = {}
        self._pending_commands: dict[str, dict[str, Any]] = {}
        self._pending_cancellers: dict[str, Callable[[], None]] = {}
        self._attr_name = f"{thermostat_name} actuator problem"
        self._attr_unique_id = (
            f"{unique_id_base}_actuator_problem" if unique_id_base else None
        )

    @property
    def is_on(self) -> bool:
        """Return True when any monitored actuator is in fault."""
        return bool(self._faults)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional diagnostic context."""
        return {
            "faulty_entities": sorted(self._faults),
            "details": self._faults,
        }

    async def async_added_to_hass(self) -> None:
        """Register listeners."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                self._actuator_ids,
                self._async_device_changed_event,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ACTUATOR_DIAGNOSTICS.format(self._runtime_key),
                self._handle_actuator_signal,
            )
        )
        self._evaluate_initial_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel outstanding timers."""
        for cancel in self._pending_cancellers.values():
            cancel()
        self._pending_cancellers.clear()
        await super().async_will_remove_from_hass()

    @callback
    def _evaluate_initial_state(self) -> None:
        for entity_id in self._actuator_ids:
            self._apply_state(entity_id, self.hass.states.get(entity_id))
        self.async_write_ha_state()

    @callback
    def _handle_actuator_signal(self, payload: dict[str, Any]) -> None:
        entity_id = payload["entity_id"]
        event_type = payload.get("event_type")
        expected_state = payload.get("expected_state")

        if event_type in {"command_error", "command_skipped"}:
            self._clear_pending(entity_id)
            self._faults[entity_id] = {
                "reason": payload.get("reason"),
                "expected_state": expected_state,
                "actual_state": payload.get("actual_state"),
                "error": payload.get("error"),
            }
            self.async_write_ha_state()
            return

        if event_type != "command_sent":
            return

        current_state = self.hass.states.get(entity_id)
        if current_state is not None and current_state.state == expected_state:
            self._clear_pending(entity_id)
            self._faults.pop(entity_id, None)
            self.async_write_ha_state()
            return

        # The service call returned, but we still need an actual state change to
        # confirm the actuator followed the command. Until then we keep a pending
        # expectation and only raise a problem if the timeout expires.
        self._pending_commands[entity_id] = {
            "expected_state": expected_state,
            "reason": "command_timeout",
        }
        self._clear_pending_canceller(entity_id)
        self._pending_cancellers[entity_id] = async_call_later(
            self.hass,
            self._timeout,
            lambda _: self._async_pending_command_timed_out(entity_id),
        )
        self.async_write_ha_state()

    @callback
    def _async_pending_command_timed_out(self, entity_id: str) -> None:
        pending = self._pending_commands.get(entity_id)
        if pending is None:
            return

        state = self.hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            reason = "entity_unavailable"
            actual_state = state.state if state else None
        else:
            reason = pending["reason"]
            actual_state = state.state

        self._faults[entity_id] = {
            "reason": reason,
            "expected_state": pending.get("expected_state"),
            "actual_state": actual_state,
        }
        self._pending_commands.pop(entity_id, None)
        self._pending_cancellers.pop(entity_id, None)
        self.async_write_ha_state()

    @callback
    def _async_device_changed_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        self._apply_state(entity_id, event.data["new_state"])
        self.async_write_ha_state()

    @callback
    def _apply_state(self, entity_id: str, new_state: State | None) -> None:
        pending = self._pending_commands.get(entity_id)
        if new_state is None or new_state.state in _UNAVAILABLE_STATES:
            expected_state = pending.get("expected_state") if pending else None
            self._clear_pending(entity_id)
            self._faults[entity_id] = {
                "reason": "entity_unavailable",
                "expected_state": expected_state,
                "actual_state": new_state.state if new_state else None,
            }
            return

        if pending and new_state.state == pending.get("expected_state"):
            self._clear_pending(entity_id)
            self._faults.pop(entity_id, None)
            return

        fault = self._faults.get(entity_id)
        if fault and fault.get("reason") in {"entity_unavailable", "command_timeout"}:
            expected_state = fault.get("expected_state")
            if expected_state is None or new_state.state == expected_state:
                self._faults.pop(entity_id, None)

    @callback
    def _clear_pending(self, entity_id: str) -> None:
        self._pending_commands.pop(entity_id, None)
        self._clear_pending_canceller(entity_id)

    @callback
    def _clear_pending_canceller(self, entity_id: str) -> None:
        cancel = self._pending_cancellers.pop(entity_id, None)
        if cancel:
            cancel()


class TemperatureProgressBinarySensor(BinarySensorEntity):
    """Binary sensor that flags insufficient temperature progress.

    This sensor only evaluates while the thermostat reports active heating or
    cooling. It keeps a baseline temperature and restarts its observation timer
    whenever meaningful progress is detected. If the configured threshold is not
    reached before the timer expires, the sensor turns on.

    The fault clears automatically when temperature progress resumes or when the
    thermostat is no longer actively heating/cooling.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        thermostat_name: str,
        runtime_key: str,
        sensor_entity_id: str,
        threshold: float,
        duration: timedelta,
        unique_id_base: str | None,
    ) -> None:
        self.hass = hass
        self._runtime_key = runtime_key
        self._sensor_entity_id = sensor_entity_id
        self._threshold = threshold
        self._duration = duration
        self._baseline_temp: float | None = None
        self._baseline_action: HVACAction | str | None = None
        self._last_progress: float | None = None
        self._is_stalled = False
        self._timer_cancel: Callable[[], None] | None = None
        self._current_action: HVACAction | str | None = None
        self._attr_name = f"{thermostat_name} temperature progress problem"
        self._attr_unique_id = (
            f"{unique_id_base}_temperature_progress_problem"
            if unique_id_base
            else None
        )

    @property
    def is_on(self) -> bool:
        """Return True when temperature is not changing fast enough."""
        return self._is_stalled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return progress monitoring details."""
        return {
            "sensor_entity_id": self._sensor_entity_id,
            "threshold": self._threshold,
            "duration_seconds": int(self._duration.total_seconds()),
            "baseline_temperature": self._baseline_temp,
            "hvac_action": self._current_action,
            "last_progress": self._last_progress,
        }

    async def async_added_to_hass(self) -> None:
        """Register listeners."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CLIMATE_DIAGNOSTICS.format(self._runtime_key),
                self._handle_climate_signal,
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._sensor_entity_id],
                self._async_sensor_changed_event,
            )
        )

        snapshot = (
            self.hass.data.get(DOMAIN, {})
            .get("climate_diagnostics", {})
            .get(self._runtime_key)
        )
        if snapshot:
            self._handle_climate_signal(snapshot)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending timer."""
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_climate_signal(self, payload: dict[str, Any]) -> None:
        self._current_action = payload.get("hvac_action")
        current_temp = payload.get("current_temperature")

        if not self._is_monitored_action(self._current_action) or current_temp is None:
            self._reset_monitor(clear_fault=True)
            return

        # Start a fresh observation window whenever heating/cooling begins, or
        # when the active action changes direction.
        if (
            self._baseline_temp is None
            or self._baseline_action != self._current_action
        ):
            self._baseline_action = self._current_action
            self._baseline_temp = float(current_temp)
            self._last_progress = 0.0
            self._restart_timer()
            if self._is_stalled:
                self._is_stalled = False
            self.async_write_ha_state()

    @callback
    def _async_sensor_changed_event(self, event: Event[EventStateChangedData]) -> None:
        if not self._is_monitored_action(self._current_action):
            return

        new_temp = _state_to_float(event.data["new_state"])
        if new_temp is None:
            return

        if self._baseline_temp is None:
            self._baseline_temp = new_temp
            self._restart_timer()
            self.async_write_ha_state()
            return

        progress = self._calculate_progress(new_temp)
        self._last_progress = progress
        if progress >= self._threshold:
            self._baseline_temp = new_temp
            self._restart_timer()
            if self._is_stalled:
                self._is_stalled = False
            self.async_write_ha_state()

    @callback
    def _restart_timer(self) -> None:
        if self._timer_cancel:
            self._timer_cancel()
        self._timer_cancel = async_call_later(
            self.hass,
            self._duration,
            self._async_progress_timeout,
        )

    @callback
    def _async_progress_timeout(self, _: Any) -> None:
        if not self._is_monitored_action(self._current_action):
            self._reset_monitor(clear_fault=True)
            return

        current_temp = _state_to_float(self.hass.states.get(self._sensor_entity_id))
        if current_temp is None or self._baseline_temp is None:
            return

        progress = self._calculate_progress(current_temp)
        self._last_progress = progress
        if progress >= self._threshold:
            self._baseline_temp = current_temp
            self._restart_timer()
            if self._is_stalled:
                self._is_stalled = False
        else:
            self._is_stalled = True
        self.async_write_ha_state()

    @callback
    def _reset_monitor(self, *, clear_fault: bool) -> None:
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None
        self._baseline_temp = None
        self._baseline_action = None
        self._last_progress = None
        if clear_fault and self._is_stalled:
            self._is_stalled = False
        self.async_write_ha_state()

    @staticmethod
    def _is_monitored_action(action: HVACAction | str | None) -> bool:
        return action in (HVACAction.HEATING, HVACAction.COOLING)

    def _calculate_progress(self, current_temp: float) -> float:
        if self._baseline_temp is None:
            return 0.0
        if self._current_action == HVACAction.HEATING:
            return current_temp - self._baseline_temp
        if self._current_action == HVACAction.COOLING:
            return self._baseline_temp - current_temp
        return 0.0


def _state_to_float(state: State | None) -> float | None:
    """Convert an entity state to float when possible."""
    if state is None or state.state in _UNAVAILABLE_STATES:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None
