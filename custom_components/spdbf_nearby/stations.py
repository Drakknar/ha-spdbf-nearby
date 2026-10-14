"""Stops, boards and the rules for treating them as one station.

No network access here: dbf.py fetches, this module decides what is the
same station, what a line is called, and how results are ordered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import re
from typing import Any

from homeassistant.util.location import distance as geo_distance

from .const import (
    MERGE_MIN_DEPARTURES,
    MERGE_NETWORK_MAX_METERS,
    RAIL_NAME_PREFIXES,
    SOURCE_DB,
)

type Position = tuple[float, float]

# Line types written together with their number: "S 5" is S5, "RE 1" RE1.
# Long-distance trains keep the space: "ICE 1107".
_JOINED_TYPES = {"S", "U", "RE", "RB", "IRE", "MEX", "RS"}


@dataclass
class Board:
    """Departures of one stop."""

    departures: list[dict[str, Any]] = field(default_factory=list)
    # Why no departures could be fetched, if no source had any.
    error: str | None = None
    # HAFAS service the departures came from, when it differs from the source.
    via: str | None = None
    # The departures came from the public fallback instance.
    fallback: bool = False
    # No server could be asked (the error says why).
    unreachable: bool = False


@dataclass
class Stop:
    """A stop returned by DBF's /_geolocation endpoint."""

    name: str
    eva: str
    source: str
    latitude: float
    longitude: float
    board: Board = field(default_factory=Board)
    # Names of nearby stops merged into this one: the same board, or the same
    # station in another network.
    merged: list[str] = field(default_factory=list)
    # The same station in other networks (DB for a BVG station). Their boards
    # add the lines this stop's own board doesn't have.
    linked: list[Stop] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Return a key that is unique across sources."""
        return f"{self.source}_{self.eva}"

    @property
    def networks(self) -> list[str]:
        """Return the networks whose departures this stop shows."""
        return list(dict.fromkeys([self.source, *(s.source for s in self.linked)]))

    @property
    def kind(self) -> str:
        """Return the marker kind: train, bus or stop."""
        if self.source == SOURCE_DB or self.name.startswith(RAIL_NAME_PREFIXES):
            return "train"
        if self.source == "BVG":
            return "bus"
        return "stop"


def tidy_line(train: Any, number: Any = None) -> str:
    """Return the line name as passengers know it.

    DB (IRIS) writes the train type before the line: "S S41", "S 5",
    "OE RB10", "STR M10", or only "ICE" with the number apart. HAFAS
    (BVG) already writes "S41", "RB10", "M10". Both must match, or the
    DB + BVG merge shows S-Bahn trains twice.
    """
    parts = str(train or "").split()
    if not parts:
        return str(number or "")
    if len(parts) == 1:
        if number and parts[0].isalpha():
            return f"{parts[0]} {number}"  # "ICE" + 2792
        return parts[0]
    kind, line = parts[0], parts[1]
    if re.fullmatch(r"[A-Za-z]+\d+[A-Za-z]?", line):
        return line  # "S S41", "OE RB10", "STR M10"
    if kind.upper() in _JOINED_TYPES and line.isdigit():
        return kind + line  # "S 5", "RE 1"
    return " ".join(parts)  # "ICE 1107", "FLX 10"


def line_key(line: Any) -> str:
    """Return a line name comparable across networks ("S7" and "S 7")."""
    return re.sub(r"\s+", "", str(line or "")).upper()


def search_text(text: str) -> str:
    """Return text for matching a typed query: lower case, words only."""
    text = re.sub(r"stra(ß|ss)e\b|str\.", "str", text.lower())
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def station_names(name: str, *, drop_city: bool) -> set[str]:
    """Return comparable forms of a station name.

    DB names start with the town ("Berlin Savignyplatz", "Berlin-Spandau"),
    BVG names end with it in brackets and start with the rail mode
    ("S Savignyplatz (Berlin)", "S+U Friedrichstr. Bhf (Berlin)"). With
    drop_city, the form without the first word (the town) is included too.
    """
    n = name.lower()
    n = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", n)  # [Bus], (Berlin), (S), (tief)
    n = re.sub(r"^\s*(s\+u|s|u)\s+", "", n)
    n = re.sub(r"stra(ß|ss)e\b|str\.", "str", n)
    n = re.sub(r"\bhbf\b", "hauptbahnhof", n)
    n = re.sub(r"\bbhf\b", " ", n)
    words = re.sub(r"[^\w]+", " ", n).split()
    forms = {" ".join(words)}
    if drop_city and len(words) > 1:
        forms.add(" ".join(words[1:]))
    return forms - {""}


def link_networks(stops: list[Stop]) -> list[Stop]:
    """Link each DB station to the BVG (HAFAS) rail stop of the same station.

    The HAFAS stop stays: its board has the U-Bahn, trams and buses, and the
    S-Bahn like DB's. The DB station joins its merged names and only adds
    the lines missing there (regional and long-distance trains). Bus and
    tram stops never qualify, so "Savignyplatz (Berlin)", the bus stop,
    stays separate from the station.
    """
    hafas_rail = [
        stop
        for stop in stops
        if stop.source != SOURCE_DB and stop.name.startswith(RAIL_NAME_PREFIXES)
    ]
    kept: list[Stop] = []
    for stop in stops:
        if stop.source == SOURCE_DB:
            names = station_names(stop.name, drop_city=True)
            matches = []
            for other in hafas_rail:
                if not station_names(other.name, drop_city=False) & names:
                    continue
                dist = geo_distance(
                    stop.latitude, stop.longitude, other.latitude, other.longitude
                )
                if dist is not None and dist <= MERGE_NETWORK_MAX_METERS:
                    matches.append((dist, other))
            if matches:
                target = min(matches, key=lambda item: item[0])[1]
                target.linked.append(stop)
                target.merged.extend([stop.name, *stop.merged])
                continue
        kept.append(stop)
    return kept


def combine_boards(own: Board, others: list[Board], keep: int) -> Board:
    """Add the lines of other networks' boards that the own board lacks.

    DB and BVG both list the S-Bahn; taking each line from one board only
    avoids showing a train twice, without matching individual trips.
    """
    if own.unreachable:
        return own
    departures = list(own.departures)
    lines = {line_key(dep["line"]) for dep in departures}
    for other in others:
        added = [dep for dep in other.departures if line_key(dep["line"]) not in lines]
        lines |= {line_key(dep["line"]) for dep in added}
        departures.extend(added)
    departures.sort(key=lambda dep: dep["minutes"])
    return Board(
        departures=departures[:keep],
        error=None if departures else own.error,
        via=own.via,
        fallback=own.fallback,
    )


def board_signature(stop: Stop) -> tuple[Any, ...] | None:
    """Return what identifies a stop's board, if it has enough departures."""
    if len(stop.board.departures) < MERGE_MIN_DEPARTURES:
        return None
    return (
        stop.source,
        *(
            (dep["line"], dep["destination"], dep["scheduled"])
            for dep in stop.board.departures
        ),
    )


def merge_same_board(stops: list[Stop]) -> list[Stop]:
    """Merge stops of one network whose departure boards are identical.

    BVG lists one station several times (S+U Alexanderplatz Bhf, .../
    Dircksenstr., .../Gontardstr., ...), and their boards are identical.
    Only exact duplicates are merged: similar names are no safe guide,
    since a tram stop with the same name can have a board of its own.
    The stops need their boards fetched; order is kept.
    """
    groups: dict[Any, list[Stop]] = {}
    for stop in stops:
        groups.setdefault(board_signature(stop) or stop.key, []).append(stop)
    merged = []
    for group in groups.values():
        # The shortest name is usually the station itself, without the
        # street of its exit or bus bay.
        keep = min(group, key=lambda stop: len(stop.name))
        keep.merged = [stop.name for stop in group if stop is not keep]
        merged.append(keep)
    return merged


def rank_search(
    stops: list[Stop], query: str, distance: Callable[[Stop], float | None]
) -> list[Stop]:
    """Order search results: the station itself, then names containing the
    query, then the rest; nearest first within each group.

    DB's station list matches loosely ("Neanderthal" for "Alexanderplatz"),
    and DBF lists DB before BVG, so DBF's own order is no guide.
    """
    wanted = search_text(query)
    wanted_names = station_names(query, drop_city=False)

    def rank(stop: Stop) -> tuple[int, bool, float]:
        # Only DB names start with the town; in BVG's the first word is
        # part of the name ("Uhlandstr./Kantstr.").
        members = [stop, *stop.linked]
        if any(
            station_names(s.name, drop_city=s.source == SOURCE_DB) & wanted_names
            for s in members
        ):
            tier = 0  # the station itself: "S+U Alexanderplatz Bhf (Berlin)"
        elif any(wanted in search_text(s.name) for s in members):
            tier = 1  # e.g. its exits: "S+U Alexanderplatz Bhf/Gontardstr."
        else:
            tier = 2
        meters = distance(stop)
        # Among equal names, a DB + BVG station before a bus bay.
        return tier, not stop.linked, meters if meters is not None else float("inf")

    return sorted(stops, key=rank)
