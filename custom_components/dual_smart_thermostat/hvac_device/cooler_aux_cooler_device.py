from datetime import timedelta
import logging

from homeassistant.components.climate import HVACMode
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt

from ..hvac_action_reason.hvac_action_reason import HVACActionReason
from ..hvac_device.multi_hvac_device import MultiHvacDevice
from ..managers.environment_manager import EnvironmentManager
from ..managers.feature_manager import FeatureManager
from ..managers.opening_manager import OpeningManager

_LOGGER = logging.getLogger(__name__)


class CoolerAUXCoolerDevice(MultiHvacDevice):
    """Two-stage cooling controller.

    Stage 1 handles normal cooling. Stage 2 is enabled after the stage 1
    cooling call remains active for the configured timeout.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        devices: list,
        initial_hvac_mode: HVACMode,
        environment: EnvironmentManager,
        openings: OpeningManager,
        features: FeatureManager,
    ) -> None:
        super().__init__(
            hass, devices, initial_hvac_mode, environment, openings, features
        )

        self._device_type = self.__class__.__name__
        self.cooler_device = devices[0]
        self.aux_cooler_device = devices[1]
        self._aux_cooler_timeout = self._features.aux_cooler_timeout
        self._aux_cooler_dual_mode = self._features.aux_cooler_dual_mode

    @property
    def _target_env_attr(self) -> str:
        return "_target_temp_high" if self._features.is_range_mode else "_target_temp"

    async def async_control_hvac(self, time=None, force=False):
        _LOGGER.debug({self.__class__.__name__})
        match self._hvac_mode:
            case HVACMode.COOL:
                await self.async_control_devices(time, force)
            case HVACMode.OFF:
                await self.async_turn_off()
            case _:
                _LOGGER.warning("Invalid HVAC mode: %s", self._hvac_mode)

    async def async_control_devices(self, time=None, force=False):
        _LOGGER.debug("async_control_devices at: %s", dt.utcnow())
        _LOGGER.debug("is_active: %s", self.is_active)
        if self.is_active:
            await self._async_control_devices_when_on(time)
        else:
            await self._async_control_devices_when_off(time)

    async def async_control_devices_forced(self, time=None) -> None:
        """Control the cooler and aux cooler when forced."""
        _LOGGER.debug("Forced control of cooling devices")
        await self.async_control_devices(time, force=True)

    async def _async_control_devices_when_off(self, time=None) -> None:
        """Check if cooling needs to start while both stages are off."""
        _LOGGER.debug("%s Controlling hvac while off", self.__class__.__name__)

        too_hot = self.environment.is_too_hot(self._target_env_attr)
        any_opening_open = self.openings.any_opening_open(self.hvac_mode)

        _LOGGER.debug(
            "_target_env_attr: %s, too_hot: %s, any_opening_open: %s, time: %s",
            self._target_env_attr,
            too_hot,
            any_opening_open,
            time,
        )

        if too_hot and not any_opening_open:
            await self.cooler_device.async_turn_on()
            self._hvac_action_reason = HVACActionReason.TARGET_TEMP_NOT_REACHED

            _LOGGER.info("Scheduling aux cooler check")
            self.async_on_remove(
                async_call_later(
                    self.hass,
                    self._aux_cooler_timeout,
                    self.async_control_devices_forced,
                )
            )

        elif time is not None or any_opening_open:
            if self.cooler_device.is_active:
                _LOGGER.info(
                    "Keep-alive - Turning off cooler %s",
                    self.cooler_device.entity_id,
                )
                await self.cooler_device.async_turn_off()
            if self.aux_cooler_device.is_active:
                _LOGGER.info(
                    "Keep-alive - Turning off aux cooler %s",
                    self.aux_cooler_device.entity_id,
                )
                await self.aux_cooler_device.async_turn_off()

            if any_opening_open:
                self._hvac_action_reason = HVACActionReason.OPENING

        else:
            _LOGGER.debug("No case matched - keep cooling devices off")

    async def _async_control_devices_when_on(self, time=None) -> None:
        """Check if cooling should continue, stop, or move to stage 2."""
        _LOGGER.debug("%s Controlling hvac while on", self.__class__.__name__)

        too_cold = self.environment.is_too_cold(self._target_env_attr)
        any_opening_open = self.openings.any_opening_open(self.hvac_mode)
        first_stage_timed_out = self._first_stage_cooling_timed_out()

        _LOGGER.debug(
            "too_cold: %s, any_opening_open: %s, time: %s",
            too_cold,
            any_opening_open,
            time,
        )
        _LOGGER.info(
            "_first_stage_cooling_timed_out: %s",
            first_stage_timed_out,
        )
        _LOGGER.debug("aux_cooler_timeout: %s", self._aux_cooler_timeout)
        _LOGGER.debug(
            "aux_cooler_device.is_active: %s", self.aux_cooler_device.is_active
        )

        if too_cold or any_opening_open:
            _LOGGER.info("Turning off coolers when on")
            await self.cooler_device.async_turn_off()
            await self.aux_cooler_device.async_turn_off()

            if too_cold:
                self._hvac_action_reason = HVACActionReason.TARGET_TEMP_REACHED
            if any_opening_open:
                self._hvac_action_reason = HVACActionReason.OPENING

        elif first_stage_timed_out and not self.aux_cooler_device.is_active:
            _LOGGER.debug("Turning on aux cooler %s", self.aux_cooler_device.entity_id)
            if not self._aux_cooler_dual_mode:
                await self.cooler_device.async_turn_off()
            await self.aux_cooler_device.async_turn_on()
            self._hvac_action_reason = HVACActionReason.TARGET_TEMP_NOT_REACHED

        elif self.aux_cooler_device.is_active and not self._aux_cooler_dual_mode:
            self._hvac_action_reason = HVACActionReason.TARGET_TEMP_NOT_REACHED

        else:
            cooler_was_active = self.cooler_device.is_active
            await self.cooler_device.async_control_hvac(time, force=False)
            self._hvac_action_reason = self.cooler_device.HVACActionReason
            if (
                cooler_was_active
                and not self.cooler_device.is_active
                and self.aux_cooler_device.is_active
            ):
                _LOGGER.info("Primary cooler turned off, also turning off aux cooler")
                await self.aux_cooler_device.async_turn_off()

    def _first_stage_cooling_timed_out(self, timeout=None) -> bool:
        """Determines if the cooler switch has been on for the timeout period."""
        if timeout is None:
            timeout = self._aux_cooler_timeout - timedelta(seconds=1)

        return condition.state(
            self.hass,
            self.cooler_device.entity_id,
            STATE_ON,
            timeout,
        )
