from datetime import datetime

from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
)

from .const import DOMAIN


# Numeric sensors sourced from the controller_status page.
# Each tuple: (controller_status metric label, entity_key, friendly name,
#             device_class, unit, conversion_factor)
# conversion_factor scales the raw value (e.g., mA → A is 0.001).
CONTROLLER_METRICS = [
    ("Battery Voltage", "battery_voltage", "Battery voltage",
     SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, 1.0),
    ("Battery Current", "battery_current", "Battery current",
     SensorDeviceClass.CURRENT, UnitOfElectricCurrent.MILLIAMPERE, 1.0),
    ("Aux. Voltage", "aux_voltage", "Aux voltage",
     SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, 1.0),
    ("Aux. Current", "aux_current", "Aux current",
     SensorDeviceClass.CURRENT, UnitOfElectricCurrent.MILLIAMPERE, 1.0),
]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up SPC sensor entities from a config entry."""

    data = hass.data[DOMAIN][entry.entry_id]

    coordinator = data["coordinator"]
    get_zone_device_info = data["get_zone_device_info"]
    alarm_device_info = data["alarm_device_info"]
    unique_prefix = data["unique_prefix"]

    # Per-zone entities (existing behaviour)
    for zone in coordinator.data["zones"].values():
        device_info = get_zone_device_info(zone)
        async_add_entities([
            SPCZoneInput(
                coordinator=coordinator,
                device_info=device_info,
                unique_prefix=unique_prefix,
                zone=zone,
            ),
            SPCZoneStatus(
                coordinator=coordinator,
                device_info=device_info,
                unique_prefix=unique_prefix,
                zone=zone,
            ),
        ])

    # Controller-status numeric sensors
    metrics = (coordinator.data.get("controller_status") or {}).get("metrics") or {}
    for label, key, friendly, devclass, unit, factor in CONTROLLER_METRICS:
        if label in metrics:
            async_add_entities([
                SPCControllerMetric(
                    coordinator=coordinator,
                    device_info=alarm_device_info,
                    unique_prefix=unique_prefix,
                    metric_label=label,
                    entity_key=key,
                    name=friendly,
                    device_class=devclass,
                    unit=unit,
                    factor=factor,
                )
            ])

    # Last-event sensor (always added; it'll just be unavailable if log fetch
    # has been failing for the entire session).
    async_add_entities([
        SPCLastEvent(
            coordinator=coordinator,
            device_info=alarm_device_info,
            unique_prefix=unique_prefix,
        )
    ])


class SPCZoneInput(CoordinatorEntity, SensorEntity):
    """Enum sensor representing SPC input state."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_has_entity_name = True
    _attr_translation_key = "zone_input"

    # XXX possibly incomplete, see pyspcwebgw.const.ZoneInput
    _attr_options = [
        "closed",       # CLOSED
        "open",         # OPEN
        "short",        # SHORT
        "discon",       # DISCONNECTED
                        # PIRMASKED
                        # DC_SUBSTITUTION
                        # SENSOR_MISSING
        "offline",      # OFFLINE
    ]

    def __init__(self, coordinator, device_info, unique_prefix, zone):
        super().__init__(coordinator)

        zone_id = zone["zone_id"]

        self._zone_id = zone_id
        self._attr_name = "Input"
        self._attr_unique_id = f"{unique_prefix}-zone{zone_id}-input"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        zone = self.coordinator.data["zones"].get(self._zone_id)
        if zone:
            return zone["input"]


class SPCZoneStatus(CoordinatorEntity, SensorEntity):
    """Enum sensor representing SPC zone status."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_has_entity_name = True
    _attr_translation_key = "zone_status"

    # XXX possibly incomplete, see pyspcwebgw.const.ZoneStatus
    _attr_options = [
        "normal",           # OK
        "inhibit",          # INHIBIT
                            # ISOLATE
                            # SOAK
        "tamper",           # TAMPER
        "actuated",         # ALARM
                            # OK_NOT_RECENT
                            # TROUBLE
    ]

    def __init__(self, coordinator, device_info, unique_prefix, zone):
        super().__init__(coordinator)

        zone_id = zone["zone_id"]

        self._zone_id = zone_id
        self._attr_name = "Status"
        self._attr_unique_id = f"{unique_prefix}-zone{zone_id}-status"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        zone = self.coordinator.data["zones"].get(self._zone_id)
        if zone:
            return zone["status"]


class SPCControllerMetric(CoordinatorEntity, SensorEntity):
    """Numeric metric from the controller_status page (battery V/A etc.)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_info, unique_prefix,
                 metric_label, entity_key, name, device_class, unit, factor):
        super().__init__(coordinator)
        self._metric_label = metric_label
        self._factor = factor
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{unique_prefix}-{entity_key}"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        cs = self.coordinator.data.get("controller_status") or {}
        m = (cs.get("metrics") or {}).get(self._metric_label)
        if m is None:
            return None
        value, _unit = m
        return value * self._factor


class SPCLastEvent(CoordinatorEntity, SensorEntity):
    """Sensor whose state is the timestamp of the most recent panel event.

    The recent events list is exposed as the `events` extra-state attribute
    (capped to the most recent 20). Useful for HA automations that react to
    the panel's own event log without polling it separately.
    """

    _attr_has_entity_name = True
    _attr_name = "Last event"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_info, unique_prefix):
        super().__init__(coordinator)
        self._attr_unique_id = f"{unique_prefix}-last-event"
        self._attr_device_info = device_info

    @staticmethod
    def _parse_dt(s):
        # Panel format: "DD/MM/YYYY HH:MM:SS" — local time on the panel.
        try:
            return datetime.strptime(s, "%d/%m/%Y %H:%M:%S").astimezone()
        except (ValueError, TypeError):
            return None

    @property
    def native_value(self):
        events = self.coordinator.data.get("events") or []
        if not events:
            return None
        return self._parse_dt(events[0]["timestamp"])

    @property
    def extra_state_attributes(self):
        events = self.coordinator.data.get("events") or []
        return {
            "events": events[:20],
            "event_count_in_view": len(events),
        }
