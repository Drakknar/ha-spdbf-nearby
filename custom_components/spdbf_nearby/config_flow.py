"""Config flow for spdbf Public Transport."""

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DEPARTURES,
    CONF_FALLBACK,
    CONF_MAP_STOPS,
    CONF_MAX_STOPS,
    CONF_MERGE_DUPLICATES,
    CONF_MERGE_NETWORKS,
    CONF_PROBLEM_AFTER,
    CONF_PUBLIC_URL,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    CONF_SERVER_URL,
    CONF_SOURCES,
    CONF_TRACKED_ENTITY,
    DEFAULT_DEPARTURES,
    DEFAULT_FALLBACK,
    DEFAULT_MAP_STOPS,
    DEFAULT_MAX_STOPS,
    DEFAULT_MERGE_DUPLICATES,
    DEFAULT_MERGE_NETWORKS,
    DEFAULT_PROBLEM_AFTER,
    DEFAULT_RADIUS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SOURCES,
    DOMAIN,
    HAFAS_SOURCES,
    REQUEST_TIMEOUT,
    SOURCE_DB,
)

# Berlin Hbf, only used to check that the server answers.
_PROBE = {"lat": "52.5251", "lon": "13.3694"}


def _schema(hass: HomeAssistant, values: dict[str, Any]) -> vol.Schema:
    tracked = values.get(CONF_TRACKED_ENTITY) or next(
        iter(hass.states.async_entity_ids("person")), None
    )
    url = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
    server = values.get(CONF_SERVER_URL)
    public = values.get(CONF_PUBLIC_URL)
    return vol.Schema(
        {
            (
                vol.Required(CONF_SERVER_URL, default=server)
                if server
                else vol.Required(CONF_SERVER_URL)
            ): url,
            # Suggested, not a default: a default would come back when the
            # field is cleared, so the public URL could never be removed.
            vol.Optional(CONF_PUBLIC_URL, description={"suggested_value": public}): url,
            (
                vol.Required(CONF_TRACKED_ENTITY, default=tracked)
                if tracked
                else vol.Required(CONF_TRACKED_ENTITY)
            ): EntitySelector(
                EntitySelectorConfig(domain=["person", "device_tracker"])
            ),
            vol.Required(
                CONF_MAP_STOPS, default=values.get(CONF_MAP_STOPS, DEFAULT_MAP_STOPS)
            ): BooleanSelector(),
            vol.Required(
                CONF_SOURCES, default=values.get(CONF_SOURCES, DEFAULT_SOURCES)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[SOURCE_DB, *HAFAS_SOURCES],
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_RADIUS, default=values.get(CONF_RADIUS, DEFAULT_RADIUS)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.2,
                    max=5,
                    step=0.1,
                    unit_of_measurement="km",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_MAX_STOPS, default=values.get(CONF_MAX_STOPS, DEFAULT_MAX_STOPS)
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=30, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_DEPARTURES, default=values.get(CONF_DEPARTURES, DEFAULT_DEPARTURES)
            ): NumberSelector(
                NumberSelectorConfig(min=0, max=10, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_MERGE_DUPLICATES,
                default=values.get(CONF_MERGE_DUPLICATES, DEFAULT_MERGE_DUPLICATES),
            ): BooleanSelector(),
            vol.Required(
                CONF_MERGE_NETWORKS,
                default=values.get(CONF_MERGE_NETWORKS, DEFAULT_MERGE_NETWORKS),
            ): BooleanSelector(),
            vol.Required(
                CONF_FALLBACK, default=values.get(CONF_FALLBACK, DEFAULT_FALLBACK)
            ): BooleanSelector(),
            vol.Required(
                CONF_PROBLEM_AFTER,
                default=values.get(CONF_PROBLEM_AFTER, DEFAULT_PROBLEM_AFTER),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=120,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=values.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=30,
                    max=600,
                    step=10,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _with_public_url(user_input: dict[str, Any]) -> dict[str, Any]:
    """Store the public URL also when cleared: empty means the server URL (an omitted key would let setup's value show through the options)."""
    return {
        **user_input,
        CONF_PUBLIC_URL: str(user_input.get(CONF_PUBLIC_URL) or "").strip(),
    }


async def _validate(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    if not user_input[CONF_SOURCES]:
        return {CONF_SOURCES: "no_sources"}
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            f"{user_input[CONF_SERVER_URL].rstrip('/')}/_geolocation",
            data=_PROBE,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
    except aiohttp.ClientError, TimeoutError, ValueError:
        return {CONF_SERVER_URL: "cannot_connect"}
    if "candidates" not in payload:
        return {CONF_SERVER_URL: "not_dbf"}
    return {}


class SpdbfConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for spdbf Public Transport."""

    VERSION = 1
    # 2: Transitous replaced the public DBF fallback (fallback_url is gone).
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match(
                {CONF_TRACKED_ENTITY: user_input[CONF_TRACKED_ENTITY]}
            )
            errors = await _validate(self.hass, user_input)
            if not errors:
                state = self.hass.states.get(user_input[CONF_TRACKED_ENTITY])
                who = state.name if state else user_input[CONF_TRACKED_ENTITY]
                return self.async_create_entry(
                    title=f"Stops near {who}", data=_with_public_url(user_input)
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(self.hass, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SpdbfOptionsFlow()


class SpdbfOptionsFlow(OptionsFlow):
    """Change any setting after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate(self.hass, user_input)
            current_server = {
                **self.config_entry.data,
                **self.config_entry.options,
            }.get(CONF_SERVER_URL)
            # Your server may be down right now, which is when you'd want to
            # change the fallback. Only a new server URL must answer.
            if (
                errors == {CONF_SERVER_URL: "cannot_connect"}
                and user_input[CONF_SERVER_URL] == current_server
            ):
                errors = {}
            if not errors:
                return self.async_create_entry(data=_with_public_url(user_input))
        current = {
            **self.config_entry.data,
            **self.config_entry.options,
            **(user_input or {}),
        }
        return self.async_show_form(
            step_id="init", data_schema=_schema(self.hass, current), errors=errors
        )
