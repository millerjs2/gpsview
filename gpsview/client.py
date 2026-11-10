"""gpsd: the server string, ?WATCH, and the JSON decoder.

No gpsd Python package is needed.  The gpsd feed is a TCP transport plus GpsdDecoder.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .feeds import Event, Feed, note, obj, raw
from .model import API_MAJOR, API_MINOR
from .transports import TcpTransport

DEFAULT_HOST = "localhost"
DEFAULT_PORT = "2947"


@dataclass(frozen=True)
class Source:
    host: str
    port: str
    device: str | None
    spec: str

    @property
    def label(self) -> str:
        """What the status bar and header show as the link."""
        return f"{self.host}:{self.port}"


def parse_source(spec: str | None) -> Source:
    """gpsd's gpsd_source_spec(): 'server[:port[:device]]', '[ipv6]:port:device', or '/dev/x'."""
    if not spec:
        return Source(DEFAULT_HOST, DEFAULT_PORT, None, f"{DEFAULT_HOST}:{DEFAULT_PORT}")
    if spec.startswith("/"):
        return Source(DEFAULT_HOST, DEFAULT_PORT, spec, spec)
    if spec.startswith("[") and "]" in spec:
        close = spec.index("]")
        host = spec[1:close]
        rest = spec[close + 1:]
        rest = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, rest = spec.partition(":")
    port, _, device = rest.partition(":")
    return Source(host or DEFAULT_HOST, port or DEFAULT_PORT, device or None, spec)


def watch_command(device: str | None) -> bytes:
    """?WATCH with JSON on. A device name limits the watch to that device."""
    watch: dict = {"enable": True, "json": True}
    if device:
        watch["device"] = device
    return ("?WATCH=" + json.dumps(watch, separators=(",", ":")) + "\n").encode("ascii")


class GpsdDecoder:
    """One JSON object per line.  Sends ?WATCH when the socket opens."""

    protocol = "gpsd"
    error_label = ""
    errors = 0

    def __init__(self, device: str | None) -> None:
        self._device = device
        self._buf = b""

    def on_open(self) -> bytes:
        return watch_command(self._device)

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").rstrip("\r")
            events.append(raw(text))
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            events.append(obj(value))
            if value.get("class") == "VERSION":
                events.append(_version_note(value))
        return events


def _version_note(value: dict) -> Event:
    release = str(value.get("release", ""))
    major, minor = value.get("proto_major"), value.get("proto_minor")
    if major != API_MAJOR or minor != API_MINOR:
        return note(f"WARNING: gpsd API {major}.{minor} differs from the expected "
                    f"{API_MAJOR}.{API_MINOR} (release {release})", "yellow")
    return note(f"gpsd {release}, API {major}.{minor}", "green")


def gpsd_feed(source: Source) -> Feed:
    return Feed(
        title="gpsd",
        label=source.label,
        header=None,
        make_transport=lambda: TcpTransport(source.host, source.port),
        make_decoder=lambda: GpsdDecoder(source.device),
        protocol="gpsd",
    )
