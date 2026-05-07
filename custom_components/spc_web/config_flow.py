import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_URL,
    CONF_USERID,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    CONF_VERIFY_SSL,
    CONF_LEGACY_SSL,
    CONF_FAST_POLL_INTERVAL,
    CONF_FAST_POLL_ZONES,
    CONF_FAST_POLL_ENABLE_ENTITY,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_FAST_POLL_ZONES,
    DEFAULT_FAST_POLL_ENABLE_ENTITY,
)
from .spc import (
    create_spc_session,
    create_legacy_ssl_spc_session,
    SPCLoginError,
)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USERID): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.Coerce(int),
        vol.Required(CONF_VERIFY_SSL, default=True): bool,
        vol.Required(CONF_LEGACY_SSL, default=False): bool,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.Coerce(int),
        # Fast-poll specific zones (e.g., motion PIRs) for sub-second
        # automation latency. Comma-separated zone IDs (e.g., "6,15,16").
        # Empty disables the fast coordinator entirely.
        vol.Optional(CONF_FAST_POLL_ZONES, default=DEFAULT_FAST_POLL_ZONES): str,
        vol.Optional(CONF_FAST_POLL_INTERVAL, default=DEFAULT_FAST_POLL_INTERVAL): vol.Coerce(int),
        # When set (e.g. "input_boolean.spc_fast_poll_enabled"), the
        # fast coordinator skips its HTTP cycle while the gate is "off".
        vol.Optional(CONF_FAST_POLL_ENABLE_ENTITY, default=DEFAULT_FAST_POLL_ENABLE_ENTITY): str,
    }
)


class SPCConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle user setup of the integration."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            url = user_input[CONF_URL]
            userid = user_input[CONF_USERID]
            password = user_input[CONF_PASSWORD]
            poll_interval = user_input[CONF_POLL_INTERVAL]
            verify_ssl = user_input[CONF_VERIFY_SSL]
            legacy_ssl = user_input[CONF_LEGACY_SSL]

            if legacy_ssl:
                spc = create_legacy_ssl_spc_session(url, userid, password)
                close_spc = spc.client.aclose
            else:
                spc = create_spc_session(self.hass, url, userid, password,
                                         verify_ssl=verify_ssl)
                close_spc = None

            try:
                await spc.login()

            except SPCLoginError:
                errors["base"] = "auth_failed"

            except Exception as error:
                errors["base"] = str(error)

            finally:
                if close_spc:
                    await close_spc()

            if not errors:
                await self.async_set_unique_id(spc.serial_number)

                return self.async_create_entry(
                    title=url,
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SPCOptionsFlow()


class SPCOptionsFlow(config_entries.OptionsFlowWithReload):
    """Options flow to tweak polling interval after setup."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
