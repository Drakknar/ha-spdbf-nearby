"""Assist tools: departures, line status, nearby stops, connections and commutes.

Home Assistant (2026.10+) asks each integration's llm platform for tools;
these join the Assist API, so conversation agents (and Home Assistant's MCP
server) can call them. They only read. The answers come from assist.py.
"""

from collections.abc import Callable
from typing import Any, override

import probatio

from homeassistant.components.llm import LLMTools
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.llm import (
    LLM_API_ASSIST,
    LLMContext,
    Tool,
    ToolAnnotations,
    ToolInput,
    ToolResult,
)

from . import assist
from .const import ASSIST_MAX_OPTIONS, ASSIST_MAX_ROWS, ASSIST_MAX_WINDOW, DOMAIN
from .coordinator import SpdbfCoordinator
from .dbf import DbfError

PROMPT = (
    "For public transport use the spdbf_nearby tools: departures of a stop, "
    "stops nearby, whether lines run or are disrupted, a connection with "
    "changes, and the user's commutes from their dashboard (\"my train to "
    'work"). Without a stop they use the stop nearest to the user. Platforms '
    "are directions at train stations. When an answer's `data` isn't "
    '"Live.", say so.'
)

_STOP = probatio.Optional(
    "stop",
    description=(
        "Stop or station name, e.g. 'Savignyplatz' or 'S Pichelsberg'. "
        "Leave out for the stop nearest to the user."
    ),
)
_NEAR = probatio.Optional(
    "near",
    description=(
        "Only when the user names a person or place to search near, e.g. "
        "'home' or 'Work' (a zone or person). Leave out for the user's location."
    ),
)
_LINES = probatio.Optional("lines", description="Only these lines, e.g. ['S3', 'M49'].")
_WITHIN = probatio.Optional(
    "within_minutes",
    description=f"How far ahead to look, in minutes (default 60, at most {ASSIST_MAX_WINDOW}).",
    default=60,
)


def _int(low: int, high: int) -> Any:
    return probatio.All(probatio.Coerce(int), probatio.Range(min=low, max=high))


# One text or a list of texts; assist.py takes either.
_TEXTS = probatio.Any(str, [str])


class _SpdbfTool(Tool):
    """A read-only spdbf tool: errors come back as a short message."""

    annotations = ToolAnnotations(
        read_only=True, destructive=False, idempotent=True, open_world=True
    )
    integration = DOMAIN

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> ToolResult:
        args = self.parameters(tool_input.tool_args)
        user_id = llm_context.context.user_id if llm_context.context else None
        try:
            coordinator = assist.coordinator_for(hass, user_id)
            data = await self._answer(hass, coordinator, args, llm_context, user_id)
        except (assist.AssistError, DbfError) as err:
            return ToolResult(data={"error": str(err)}, error=True)
        return ToolResult(data=data)

    async def _answer(
        self,
        hass: HomeAssistant,
        coordinator: SpdbfCoordinator,
        args: dict[str, Any],
        llm_context: LLMContext,
        user_id: str | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _place(
        hass: HomeAssistant,
        coordinator: SpdbfCoordinator,
        args: dict[str, Any],
        llm_context: LLMContext,
        user_id: str | None,
    ) -> assist.Place:
        return assist.locate(
            hass, coordinator, args.get("near"), llm_context.device_id, user_id
        )


class DeparturesTool(_SpdbfTool):
    """The next departures of a stop."""

    name = f"{DOMAIN}__departures"
    title = "Public transport departures"
    description = (
        "Next departures of a public transport stop: line, destination, "
        "platform, minutes until departure, delay, cancelled. Use for "
        "'when is the next S3 at Pichelsberg', 'buses to Zoologischer Garten "
        "from here', 'first 2 departures from both platforms at Savignyplatz' "
        "(per_platform 2), 'next U2 towards Pankow at Alexanderplatz' (towards)."
    )
    parameters = probatio.Schema(
        {
            _STOP: str,
            _NEAR: str,
            _LINES: _TEXTS,
            probatio.Optional(
                "destination",
                description="Only departures whose final destination contains this, e.g. 'Zoologischer Garten'.",
            ): str,
            probatio.Optional(
                "towards",
                description=(
                    "Only departures that call at this station later (also when "
                    "it isn't their final stop), e.g. 'Pankow'. Slower; prefer "
                    "destination when it is the end of the line."
                ),
            ): str,
            probatio.Optional(
                "platforms", description="Only these platforms, e.g. ['3']."
            ): _TEXTS,
            probatio.Optional(
                "per_platform",
                description="Group by platform with this many departures each, e.g. 2.",
            ): _int(1, 10),
            probatio.Optional(
                "count",
                description=f"How many departures (default 8, at most {ASSIST_MAX_ROWS}).",
                default=8,
            ): _int(1, ASSIST_MAX_ROWS),
            _WITHIN: _int(1, ASSIST_MAX_WINDOW),
        }
    )

    @override
    async def _answer(
        self,
        hass: HomeAssistant,
        coordinator: SpdbfCoordinator,
        args: dict[str, Any],
        llm_context: LLMContext,
        user_id: str | None,
    ) -> dict[str, Any]:
        return await assist.departures(
            coordinator,
            self._place(hass, coordinator, args, llm_context, user_id),
            stop=args.get("stop"),
            filters=assist.Filters.of(
                {
                    "lines": args.get("lines"),
                    "platforms": args.get("platforms"),
                    "destinations": args.get("destination"),
                }
            ),
            towards=args.get("towards"),
            per_platform=args.get("per_platform"),
            count=args["count"],
            within_minutes=args["within_minutes"],
        )


class LineStatusTool(_SpdbfTool):
    """How the lines at a stop are running."""

    name = f"{DOMAIN}__line_status"
    title = "Public transport line status"
    description = (
        "How the lines at a stop are running: next departure, cancellations, "
        "delays, disruption notices, and asked lines with no departures. Use "
        "for 'is the S41 running?', 'any cancellations at Westkreuz?', "
        "'are there delays on the U2 here?'."
    )
    parameters = probatio.Schema(
        {
            _STOP: str,
            _NEAR: str,
            _LINES: _TEXTS,
            _WITHIN: _int(1, ASSIST_MAX_WINDOW),
        }
    )

    @override
    async def _answer(
        self,
        hass: HomeAssistant,
        coordinator: SpdbfCoordinator,
        args: dict[str, Any],
        llm_context: LLMContext,
        user_id: str | None,
    ) -> dict[str, Any]:
        return await assist.line_status(
            coordinator,
            self._place(hass, coordinator, args, llm_context, user_id),
            stop=args.get("stop"),
            lines=args.get("lines"),
            within_minutes=args["within_minutes"],
        )


class NearbyStopsTool(_SpdbfTool):
    """The stops near the user."""

    name = f"{DOMAIN}__nearby_stops"
    title = "Public transport stops nearby"
    description = (
        "Public transport stops near the user (or a named person or place), "
        "nearest first, with distance and the lines seen there. Use for "
        "'what's near me?', 'which stops are close?'."
    )
    parameters = probatio.Schema(
        {
            _NEAR: str,
            probatio.Optional(
                "count", description="How many stops (default 5).", default=5
            ): _int(1, 12),
        }
    )

    @override
    async def _answer(
        self,
        hass: HomeAssistant,
        coordinator: SpdbfCoordinator,
        args: dict[str, Any],
        llm_context: LLMContext,
        user_id: str | None,
    ) -> dict[str, Any]:
        return await assist.nearby_stops(
            coordinator,
            self._place(hass, coordinator, args, llm_context, user_id),
            count=args["count"],
        )


_LEG = {
    probatio.Optional(
        "stop",
        description="Where this leg starts; leave out only on the first leg for the nearest stop.",
    ): str,
    probatio.Optional("lines", description="Lines to take, e.g. ['S5', 'S7']."): _TEXTS,
    probatio.Optional(
        "platforms", description="Platform(s) = direction, e.g. ['3']."
    ): _TEXTS,
    probatio.Optional(
        "destination", description="Final destination contains this."
    ): str,
    probatio.Optional(
        "towards", description="The line must call at this station later."
    ): str,
    probatio.Optional(
        "walk_minutes",
        description=(
            "Walk before boarding: to the first stop (default 0), or the change "
            "(default 3; 0 for the same platform)."
        ),
    ): _int(0, 60),
}


class ConnectionTool(_SpdbfTool):
    """A connection with changes, described by the user."""

    name = f"{DOMAIN}__connection"
    title = "Public transport connection"
    description = (
        "Times a journey the user describes in legs (no route search): the "
        "next departures of the first leg, ride time, arrival at the change, "
        "and the departure caught on the next leg. Use for 'if I take the next "
        "S-Bahn from Savignyplatz, which U2 do I get at Alexanderplatz "
        "towards Pankow?' (legs: Savignyplatz platform 3, then Alexanderplatz "
        "line U2 towards Pankow)."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("legs", description="The legs in order."): [_LEG],
            probatio.Optional(
                "to",
                description="Station where the last leg gets off, for its arrival time.",
            ): str,
            probatio.Optional(
                "walk_after_minutes",
                description="Walk after getting off the last leg.",
                default=0,
            ): _int(0, 60),
            _NEAR: str,
            probatio.Optional(
                "options", description="How many next options (default 2).", default=2
            ): _int(1, ASSIST_MAX_OPTIONS),
        }
    )

    @override
    async def _answer(
        self,
        hass: HomeAssistant,
        coordinator: SpdbfCoordinator,
        args: dict[str, Any],
        llm_context: LLMContext,
        user_id: str | None,
    ) -> dict[str, Any]:
        legs = [{**leg, "destinations": leg.get("destination")} for leg in args["legs"]]
        return await assist.connection(
            coordinator,
            self._place(hass, coordinator, args, llm_context, user_id),
            legs,
            args.get("to"),
            args["walk_after_minutes"],
            args["options"],
        )


class CommutesTool(_SpdbfTool):
    """The user's commutes, from the dashboard's connection cards."""

    name = f"{DOMAIN}__commutes"
    title = "Public transport commutes"
    description = (
        "The user's commutes as set up on their dashboard (sections of "
        "connection cards, e.g. 'Work via Alexanderplatz'): the next trains "
        "for each leg with ride, change and arrival times. Use for 'when's my "
        "next train to work?', 'when do I get home?'. Without a name it "
        "answers the commutes the dashboard shows right now."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "commute",
                description="Part of the commute's name, e.g. 'work' or 'Home via Ring'.",
            ): str,
            probatio.Optional(
                "options", description="How many next options (default 2).", default=2
            ): _int(1, ASSIST_MAX_OPTIONS),
        }
    )

    @override
    async def _answer(
        self,
        hass: HomeAssistant,
        coordinator: SpdbfCoordinator,
        args: dict[str, Any],
        llm_context: LLMContext,
        user_id: str | None,
    ) -> dict[str, Any]:
        return await assist.commutes(
            hass,
            coordinator,
            assist.person_state(hass, coordinator, user_id),
            name=args.get("commute"),
            options=args["options"],
        )


TOOLS: list[Callable[[], Tool]] = [
    DeparturesTool,
    LineStatusTool,
    NearbyStopsTool,
    ConnectionTool,
    CommutesTool,
]


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    """Return the tools while an entry is loaded."""
    if api_id != LLM_API_ASSIST:
        return None
    if not any(
        entry.state is ConfigEntryState.LOADED
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return None
    return LLMTools(tools=[tool() for tool in TOOLS], prompt=PROMPT)
