"""What the app reads: a transport plus a protocol decoder.

A decoder turns bytes into Events.  Each Event is one of:
    raw   a line or frame summary for the message pane (shown dim, or in the given style)
    obj   a gpsd-shaped object (TPV, SKY, ATT) for GpsState.apply()
    note  a notice for the message pane (connection state, warnings)

Every protocol produces the same gpsd-shaped objects, so the panels and the status bar
are shared.  Only the decoders know about NMEA sentences or SBP messages.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .transports import Endpoint, Transport


@dataclass(frozen=True)
class Event:
    kind: str                 # "raw", "obj" or "note"
    text: str = ""
    obj: dict | None = None
    style: str = ""


def raw(text: str, style: str = "dim") -> Event:
    return Event("raw", text=text, style=style)


def note(text: str, style: str = "") -> Event:
    return Event("note", text=text, style=style)


def obj(value: dict) -> Event:
    return Event("obj", obj=value)


class Decoder(Protocol):
    protocol: str             # short name, e.g. "gpsd"
    error_label: str          # status-bar label for the error count, or "" for none
    errors: int

    def on_open(self) -> bytes:
        """Bytes to send once the transport is open (gpsd's ?WATCH); b'' for read-only feeds."""
        ...

    def feed(self, data: bytes) -> list[Event]:
        ...


@dataclass(frozen=True)
class Feed:
    title: str                          # "gpsd", "NMEA" or "SBP": used in dialogs
    label: str                          # the link, shown in the header and status bar
    header: str | None                  # status-bar protocol text; None = gpsd's release
    make_transport: Callable[[], Transport]
    make_decoder: Callable[[], Decoder]
    endpoint: Endpoint | None = None    # where the data comes from; None for gpsd
    protocol: str = ""                  # "gpsd", "nmea", "sbp" or "ubx" (default baud rates)
