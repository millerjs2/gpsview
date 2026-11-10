"""Building a Feed from a protocol, a source string and an optional baud rate.

Shared by the command line and the in-app "Configure input" dialog, so both accept the same
forms and give the same error messages.
"""
from __future__ import annotations

import dataclasses
import glob

from .client import gpsd_feed, parse_source
from .feeds import Feed
from .nmea import nmea_feed
from .swift import sbp_feed
from .transports import DEFAULT_BAUD, FILE_RATE, parse_endpoint
from .ubx import ubx_feed

# (label for the dialog, protocol key)
PROTOCOLS = (("gpsd", "gpsd"), ("NMEA 0183", "nmea"), ("Swift SBP", "sbp"), ("u-blox UBX", "ubx"))
_FEEDS = {"nmea": nmea_feed, "sbp": sbp_feed, "ubx": ubx_feed}
BAUD_MIN, BAUD_MAX = 50, 4_000_000


AUTO = "auto"


def parse_baud(text: str | int | None) -> int | None:
    """A baud rate, or None for "the protocol's default" (and for "auto", see is_auto)."""
    if text is None or str(text).strip() == "" or is_auto(text):
        return None
    t = str(text).strip()
    if not t.isdigit() or not BAUD_MIN <= int(t) <= BAUD_MAX:
        raise ValueError(f"baud rate '{t}': give a number from {BAUD_MIN} to {BAUD_MAX}, "
                         "such as 9600, 38400 or 115200")
    return int(t)


def is_auto(text: str | int | None) -> bool:
    return str(text).strip().lower() == AUTO


def make_feed(protocol: str, spec: str, *, baud: str | int | None = None,
              file_rate: float = FILE_RATE) -> Feed:
    """Raises ValueError with a message fit to show the user."""
    rate = parse_baud(baud)
    spec = spec.strip()
    if protocol == "gpsd":
        if rate is not None or is_auto(baud):
            raise ValueError("a baud rate only applies to a tty; gpsd owns its own devices")
        return gpsd_feed(parse_source(spec or None))
    if protocol not in _FEEDS:
        raise ValueError(f"unknown protocol '{protocol}'")
    if not spec:
        raise ValueError("give a source: /dev/ttyUSB0, host:port, udp://:port or a file")
    endpoint = parse_endpoint(spec, protocol)
    if rate is not None:
        if endpoint.kind != "serial":
            raise ValueError("a baud rate only applies to a tty (/dev/ttyUSB0), not to "
                             f"{endpoint.kind}")
        if ":" in spec.rpartition("/")[2] and endpoint.baud != rate:
            raise ValueError(f"the source says {endpoint.baud} baud and the baud setting says "
                             f"{rate}: give only one")
        endpoint = dataclasses.replace(endpoint, baud=rate)
    if is_auto(baud):
        if endpoint.kind != "serial":
            raise ValueError("automatic baud rate only applies to a tty (/dev/ttyUSB0)")
        if ":" in spec.rpartition("/")[2]:
            raise ValueError("the source gives a baud rate, so there is nothing to detect: "
                             "drop the :RATE, or drop auto")
        endpoint = dataclasses.replace(endpoint, autobaud=True)
    if endpoint.kind == "file":
        endpoint = dataclasses.replace(endpoint, rate=file_rate)
    return _FEEDS[protocol](endpoint)


def baud_text(feed: Feed) -> str:
    """The baud rate for the dialog's field: a number, "auto", or blank (not a tty)."""
    ep = feed.endpoint
    if ep is None or ep.kind != "serial":
        return ""
    return AUTO if ep.autobaud else str(ep.baud)


def default_baud(protocol: str) -> int:
    return DEFAULT_BAUD.get(protocol, 0)


def feed_spec(feed: Feed) -> str:
    """The source string that would rebuild `feed` (to prefill the dialog)."""
    if feed.protocol == "gpsd":
        return feed.spec
    ep = feed.endpoint
    if ep is None:
        return feed.label
    return ep.path if ep.kind == "serial" else ep.label


def find_ttys() -> list[str]:
    """Serial devices that look like receivers, for a hint in the dialog."""
    found: list[str] = []
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*"):   # not ttyS*: mostly unwired
        found += sorted(glob.glob(pattern))
    return found
