from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)

from .const import DOMAIN


ACTUATED_NAME = {
    "alarm": "Motion",
    "entry/exit": "Contact",
    "entry/exit 2": "Contact",
    "fire": "Fire",
    "technical": "Fault",
}


ACTUATED_DEVCLASS = {
    "alarm": BinarySensorDeviceClass.MOTION,
    "entry/exit": BinarySensorDeviceClass.OPENING,
    "entry/exit 2": BinarySensorDeviceClass.OPENING,
    "fire": BinarySensorDeviceClass.SMOKE,
    "technical": BinarySensorDeviceClass.PROBLEM,
}


# Diagnostic indicators sourced from the controller_status page. Each tuple is:
#   (controller_status indicator-label, HA entity-key, friendly-name,
#    BinarySensorDeviceClass)
# The indicator-label is the exact text the panel emits before the
# `<FONT COLOR=...>` indicator and is firmware-canonical when language=0
# is sent (which the integration always does). is_on is True when color != green.
CONTROLLER_INDICATORS = [
    # Tamper indicators (5)
    ("Cabinet Tamper", "cabinet_tamper", "Cabinet tamper", BinarySensorDeviceClass.TAMPER),
    ("Aux. Tamper 1", "aux_tamper_1", "Aux tamper 1", BinarySensorDeviceClass.TAMPER),
    ("Aux. Tamper 2", "aux_tamper_2", "Aux tamper 2", BinarySensorDeviceClass.TAMPER),
    ("Bell Tamper",   "bell_tamper",   "Bell tamper",   BinarySensorDeviceClass.TAMPER),
    ("Antenna Tamper","antenna_tamper","Antenna tamper",BinarySensorDeviceClass.TAMPER),
    # Power indicators (6)
    ("Mains",            "mains",            "Mains power",       BinarySensorDeviceClass.POWER),
    ("Mains time sync.", "mains_time_sync",  "Mains time sync",   BinarySensorDeviceClass.PROBLEM),
    ("Battery",          "battery",          "Battery health",    BinarySensorDeviceClass.PROBLEM),
    ("Aux. Fuse",        "aux_fuse",         "Aux fuse",          BinarySensorDeviceClass.PROBLEM),
    ("Ext.Bell Fuse",    "ext_bell_fuse",    "External bell fuse",BinarySensorDeviceClass.PROBLEM),
    ("Int. Bell Fuse",   "int_bell_fuse",    "Internal bell fuse",BinarySensorDeviceClass.PROBLEM),
    # X-BUS indicators (9)
    ("Cable status",       "xbus_cable",        "X-BUS cable",         BinarySensorDeviceClass.PROBLEM),
    ("Devices: Comms",     "xbus_comms",        "X-BUS device comms",  BinarySensorDeviceClass.PROBLEM),
    ("Devices: Lid tamper","xbus_lid_tamper",   "X-BUS lid tamper",    BinarySensorDeviceClass.TAMPER),
    ("Devices: Ant. tamper","xbus_ant_tamper",  "X-BUS antenna tamper",BinarySensorDeviceClass.TAMPER),
    ("Devices: RF Jamming","xbus_rf_jamming",   "X-BUS RF jamming",    BinarySensorDeviceClass.PROBLEM),
    ("Devices: Fuse",      "xbus_fuse",         "X-BUS fuse",          BinarySensorDeviceClass.PROBLEM),
    ("Devices: Mains",     "xbus_mains",        "X-BUS mains",         BinarySensorDeviceClass.POWER),
    ("Devices: Battery",   "xbus_battery",      "X-BUS battery",       BinarySensorDeviceClass.PROBLEM),
    ("Devices: PSU Fault", "xbus_psu_fault",    "X-BUS PSU",           BinarySensorDeviceClass.PROBLEM),
]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up SPC binary sensor entities from a config entry."""

    data = hass.data[DOMAIN][entry.entry_id]

    coordinator = data["coordinator"]
    get_zone_device_info = data["get_zone_device_info"]
    alarm_device_info = data["alarm_device_info"]
    unique_prefix = data["unique_prefix"]

    # Per-zone entities (existing behaviour)
    for zone in coordinator.data["zones"].values():
        device_info = get_zone_device_info(zone)
        async_add_entities([
            SPCZoneActuated(
                coordinator=coordinator,
                device_info=device_info,
                unique_prefix=unique_prefix,
                zone=zone,
            ),
            SPCZoneTamper(
                coordinator=coordinator,
                device_info=device_info,
                unique_prefix=unique_prefix,
                zone=zone,
            ),
        ])

    # Controller-status binary sensors — only added when the page parsed
    # successfully and the indicator label is present in the panel response.
    # This keeps us forwards-compatible with panel models that don't expose
    # all of these indicators (e.g., no antenna on fixed-line systems).
    indicators = (coordinator.data.get("controller_status") or {}).get("indicators") or {}
    for label, key, friendly, devclass in CONTROLLER_INDICATORS:
        if label in indicators:
            async_add_entities([
                SPCControllerIndicator(
                    coordinator=coordinator,
                    device_info=alarm_device_info,
                    unique_prefix=unique_prefix,
                    indicator_label=label,
                    entity_key=key,
                    name=friendly,
                    device_class=devclass,
                )
            ])


class SPCZoneActuated(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether the SPC zone is actuated."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, device_info, unique_prefix, zone):
        super().__init__(coordinator)

        zone_id = zone["zone_id"]
        zone_type = zone["zone_type"]

        self._zone_id = zone_id
        self._attr_name = ACTUATED_NAME.get(zone_type, "Actuated")
        self._attr_device_class = ACTUATED_DEVCLASS.get(zone_type)
        self._attr_unique_id = f"{unique_prefix}-zone{zone_id}-actuated"
        self._attr_device_info = device_info

    @property
    def is_on(self):
        zone = self.coordinator.data["zones"].get(self._zone_id)
        if zone:
            return (zone["status"] == "actuated")
        return False


class SPCZoneTamper(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether the SPC zone is in a tamper state."""

    _attr_has_entity_name = True
    _attr_name = "Tamper"
    _attr_device_class = BinarySensorDeviceClass.TAMPER

    def __init__(self, coordinator, device_info, unique_prefix, zone):
        super().__init__(coordinator)

        zone_id = zone["zone_id"]

        self._zone_id = zone_id
        self._attr_unique_id = f"{unique_prefix}-zone{zone_id}-tamper"
        self._attr_device_info = device_info

    @property
    def is_on(self):
        zone = self.coordinator.data["zones"].get(self._zone_id)
        if zone:
            return (zone["status"] == "tamper")
        return False


class SPCControllerIndicator(CoordinatorEntity, BinarySensorEntity):
    """Generic problem/tamper/power indicator from the controller_status page.

    is_on is True whenever the panel renders the indicator in any colour
    other than green (typically red for "Fault" / "Tamper" / "Open").
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_info, unique_prefix,
                 indicator_label, entity_key, name, device_class):
        super().__init__(coordinator)
        self._indicator_label = indicator_label
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_unique_id = f"{unique_prefix}-{entity_key}"
        self._attr_device_info = device_info

    @property
    def is_on(self):
        cs = self.coordinator.data.get("controller_status") or {}
        ind = (cs.get("indicators") or {}).get(self._indicator_label)
        if not ind:
            return None
        return ind.get("color", "").lower() != "green"

    @property
    def extra_state_attributes(self):
        cs = self.coordinator.data.get("controller_status") or {}
        ind = (cs.get("indicators") or {}).get(self._indicator_label)
        if not ind:
            return None
        # Useful for debugging — e.g., Mains time sync shows "OK (50Hz)" so
        # users can see the frequency without us promoting it to its own
        # sensor.
        return {"raw_text": ind.get("text"), "color": ind.get("color")}
