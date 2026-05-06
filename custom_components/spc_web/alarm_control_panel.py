import logging

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)

from .const import DOMAIN
from .spc import SPCError

LOGGER = logging.getLogger(__name__)

# Maps the canonical arm-state code returned by parse_system_summary_state()
# to a Home Assistant alarm state. The codes are derived from the firmware-
# canonical form-button names (e.g., `partset_a_area1` → `partset_a`), so
# this map is locale- and installer-label-invariant.
ARM_STATE_TO_HA = {
    "unset": AlarmControlPanelState.DISARMED,
    "fullset": AlarmControlPanelState.ARMED_AWAY,
    "partset_a": AlarmControlPanelState.ARMED_NIGHT,
    "partset_b": AlarmControlPanelState.ARMED_HOME,
}


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up SPC alarm control panel entity from a config entry."""

    data = hass.data[DOMAIN][entry.entry_id]

    coordinator = data["coordinator"]
    spc = data["spc"]
    alarm_device_info = data["alarm_device_info"]
    unique_prefix = data["unique_prefix"]

    async_add_entities(
        [
            SPCAlarm(
                coordinator=coordinator,
                spc=spc,
                device_info=alarm_device_info,
                unique_prefix=unique_prefix,
            )
        ]
    )


class SPCAlarm(CoordinatorEntity, AlarmControlPanelEntity):
    """Alarm entity representing all SPC areas."""

    _attr_code_arm_required = False
    # ARM_NIGHT and ARM_AWAY are always supported (every Vanderbilt SPC has a
    # Full-set and at least Part-set A). ARM_HOME (= Part-set B) is added
    # dynamically by the supported_features property when the panel actually
    # exposes a partset_b_area1 button — see below.
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )
    _attr_has_entity_name = True

    def __init__(self, coordinator, spc, device_info, unique_prefix):
        super().__init__(coordinator)
        self.spc = spc

        self._attr_name = "Alarm"
        self._attr_unique_id = f"{unique_prefix}-alarm"
        self._attr_device_info = device_info

    @property
    def supported_features(self):
        """Add ARM_HOME only when the panel actually has Part-set B configured.

        Vanderbilt SPC supports up to 2 Part-set levels per area. Most
        installations only configure Part-set A. Without this check, HA would
        offer the user an "Arm Home" button that silently does nothing on
        such panels (the POST has no matching form field, the panel ignores
        it). We detect availability by looking for the partset_b_area1
        button in the cache populated during disarmed-state polls.
        """
        features = self._attr_supported_features
        cache = self.coordinator.data.get("button_cache") or {}
        if "partset_b_area1" in cache:
            features = features | AlarmControlPanelEntityFeature.ARM_HOME
        return features

    @property
    def alarm_state(self):
        arm_state = self.coordinator.data["arm_state"]
        ha_state = ARM_STATE_TO_HA.get(arm_state)
        if ha_state is None and arm_state is not None:
            # parse_system_summary_state may return "armed_unknown:<text>" if it
            # detects the unset button is present but can't reverse-look up the
            # status text. That happens before we've populated the button
            # cache from a disarmed-state poll — usually transient.
            LOGGER.debug(
                "Arm state %r not yet mapped (button cache may not be primed)",
                arm_state,
            )
        return ha_state

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {
            "raw_arm_state": data.get("arm_state"),
            "button_cache": data.get("button_cache"),
        }

    async def _async_set_arm_state(self, arm_state):
        try:
            await self.spc.set_arm_state(arm_state)
        except SPCError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            await self.coordinator.async_request_refresh()

    async def async_alarm_disarm(self, code=None):
        await self._async_set_arm_state("unset")

    async def async_alarm_arm_away(self, code=None):
        await self._async_set_arm_state("fullset")

    async def async_alarm_arm_night(self, code=None):
        await self._async_set_arm_state("partset_a")

    async def async_alarm_arm_home(self, code=None):
        await self._async_set_arm_state("partset_b")
