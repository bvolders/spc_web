DOMAIN = "spc_web"
MANUFACTURER = "Vanderbilt"

CONF_URL = "url"
CONF_USERID = "userid"
CONF_PASSWORD = "password"
CONF_POLL_INTERVAL = "poll_interval"
CONF_VERIFY_SSL = "verify_ssl"
CONF_LEGACY_SSL = "legacy_ssl"
# Per-zone fast polling: a separate coordinator that ONLY fetches the
# status_zones page at a much higher rate, for use as motion-trigger
# sensors. Listed zones get their "actuated" binary_sensor bound to the
# fast coordinator; everything else (alarm panel, controller status,
# tamper sensors, unlisted zones) stays on the slow coordinator.
CONF_FAST_POLL_INTERVAL = "fast_poll_interval"
CONF_FAST_POLL_ZONES = "fast_poll_zones"
# When set, the fast coordinator skips its HTTP poll cycle while this
# entity is "off" — useful to suspend per-second panel hits during
# windows where no automation will act on motion data anyway.
CONF_FAST_POLL_ENABLE_ENTITY = "fast_poll_enable_entity"

DEFAULT_POLL_INTERVAL = 30
DEFAULT_FAST_POLL_INTERVAL = 1
DEFAULT_FAST_POLL_ZONES = ""  # comma-separated zone_ids, e.g. "6,15,16"
DEFAULT_FAST_POLL_ENABLE_ENTITY = ""  # empty = always poll when zones configured

PLATFORMS = [
    "alarm_control_panel",
    "binary_sensor",
    "sensor",
    "switch",
]
