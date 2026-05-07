import logging
from datetime import timedelta

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
import httpx

from .const import (
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    CONF_URL,
    CONF_USERID,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_VERIFY_SSL,
    CONF_LEGACY_SSL,
    CONF_FAST_POLL_INTERVAL,
    CONF_FAST_POLL_ZONES,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_FAST_POLL_ZONES,
)
from .spc import (
    create_spc_session,
    create_legacy_ssl_spc_session,
    SPCError,
)


LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry):
    url = entry.data[CONF_URL]
    userid = entry.data[CONF_USERID]
    password = entry.data[CONF_PASSWORD]

    poll_seconds = entry.options.get(
        CONF_POLL_INTERVAL,
        entry.data[CONF_POLL_INTERVAL],
    )
    poll_interval = timedelta(seconds=int(poll_seconds))

    verify_ssl = entry.data[CONF_VERIFY_SSL]
    legacy_ssl = entry.data[CONF_LEGACY_SSL]

    if legacy_ssl:
        spc = create_legacy_ssl_spc_session(url, userid, password)
        close_spc = spc.session.aclose
    else:
        spc = create_spc_session(hass, url, userid, password,
                                 verify_ssl=verify_ssl)
        close_spc = None

    await spc.login()

    # Persistent across polls: maps arming-button name (e.g., "partset_a_area1")
    # to its `value=` attribute as it last appeared in a disarmed-state poll.
    # Used to reverse-look up the "All Areas" status text when the panel is
    # currently armed (the displayed text equals the value of whichever
    # arming button created this state). Surviving across polls means even
    # if the panel is armed when the integration starts, we'll begin
    # populating the cache the first time it disarms.
    button_cache: dict[str, str] = {}

    async def update():
        nonlocal button_cache
        try:
            arm_state, button_cache = await spc.get_system_summary(button_cache)
            zones = await spc.get_zones()
            controller_status = await spc.get_controller_status()
            try:
                events = await spc.get_event_log(limit=20)
            except SPCError as event_err:
                # Event log is non-essential — log and carry on.
                LOGGER.debug("event log fetch failed: %s", event_err)
                events = []
            return {
                "arm_state": arm_state,
                "button_cache": dict(button_cache),
                "zones": {zone["zone_id"]: zone for zone in zones},
                "controller_status": controller_status,
                "events": events,
            }

        except SPCError as error:
            # Treat as hard failure. Show unavailable.
            raise UpdateFailed(str(error)) from error

        except (httpx.HTTPError, ValueError) as error:
            raise UpdateFailed(f"SPC communication error: {error!s}") from error

    coordinator = DataUpdateCoordinator(
        hass,
        LOGGER,
        config_entry=entry,
        name="Vanderbilt SPC Web",
        update_interval=poll_interval,
        update_method=update,
        always_update=False,
    )
    await coordinator.async_config_entry_first_refresh()

    # Fast zone-only coordinator. Only built when fast_poll_zones is non-empty.
    # Polls /secure.htm?page=status_zones at fast_poll_interval seconds to give
    # motion-triggered automations sub-second latency without hammering the
    # rest of the panel's pages (system_summary + controller_status).
    fast_zones_raw = entry.options.get(
        CONF_FAST_POLL_ZONES, DEFAULT_FAST_POLL_ZONES,
    )
    # parse_status_zones yields zone_id as int; keep the set as int too
    # so membership checks downstream (zone["zone_id"] in fast_zone_ids)
    # match. Silently drops malformed entries.
    fast_zone_ids = set()
    for z in (fast_zones_raw or "").split(","):
        z = z.strip()
        if z.isdigit():
            fast_zone_ids.add(int(z))
    fast_coordinator = None
    if fast_zone_ids:
        fast_seconds = entry.options.get(
            CONF_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_INTERVAL,
        )
        fast_interval = timedelta(seconds=int(fast_seconds))

        async def fast_update():
            try:
                zones = await spc.get_zones()
                zone_dict = {zone["zone_id"]: zone for zone in zones}
                # TEMP DEBUG: log fast-poll cycle with status of fast zones.
                # Remove once latency is verified.
                LOGGER.info(
                    "fast tick zones=%s",
                    {zid: zone_dict[zid]["status"] for zid in sorted(fast_zone_ids) if zid in zone_dict},
                )
                return {"zones": zone_dict}
            except SPCError as error:
                raise UpdateFailed(str(error)) from error
            except (httpx.HTTPError, ValueError) as error:
                raise UpdateFailed(
                    f"SPC fast-poll communication error: {error!s}"
                ) from error

        fast_coordinator = DataUpdateCoordinator(
            hass,
            LOGGER,
            config_entry=entry,
            name="Vanderbilt SPC Web (fast zones)",
            update_interval=fast_interval,
            update_method=fast_update,
            always_update=False,
        )
        await fast_coordinator.async_config_entry_first_refresh()

    alarm_device_id = (DOMAIN, f"{spc.serial_number}-alarm")
    alarm_device_info = DeviceInfo({
        "identifiers": {alarm_device_id},
        "name": (spc.site or "SPC Panel"),
        "manufacturer": MANUFACTURER,
        "model": spc.model,
        "serial_number": spc.serial_number,
    })

    def get_zone_device_info(zone):
        return DeviceInfo({
            "identifiers": {(DOMAIN, f"{spc.serial_number}-zone{zone["zone_id"]}")},
            "name": f"Zone {zone["zone_id"]} {zone["zone_name"]}",
            "manufacturer": MANUFACTURER,
            "model": f"{spc.model} Zone",
            "via_device": alarm_device_id,
        })

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "spc": spc,
        "coordinator": coordinator,
        "fast_coordinator": fast_coordinator,
        "fast_zone_ids": fast_zone_ids,
        "alarm_device_info": alarm_device_info,
        "get_zone_device_info": get_zone_device_info,
        "unique_prefix": f"spc{spc.serial_number}",
        "close_spc": close_spc,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data and data["close_spc"]:
            await data["close_spc"]()
    return unload_ok
