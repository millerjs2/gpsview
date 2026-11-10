"""Where RTCM corrections go: a sink.

A sink takes whole RTCM frames and delivers them to a receiver.  Some receivers want
corrections on the port the client is already reading from; others need them on a different
interface (a u-blox on USB sends data on one port and may take corrections on its UART; a
Swift Piksi takes them on its own serial port).  So the destination is chosen separately
from the data source, with --rtcm-out:

    same                    the link the data comes from (a serial tty or a TCP connection;
                            for UDP, the address the datagrams arrive from)
    /dev/ttyUSB1[:baud]     a serial port
    [tcp://]host:port       a TCP connection to the receiver (or a serial-to-TCP bridge)
    udp://host:port         datagrams to that address (output only; the host is where to
                            SEND, unlike a UDP data source, where it is what to bind)

Corrections are only useful when fresh.  A sink that is down drops what it is given instead
of queueing it, counts the drops, and reconnects in the background.  Nothing here ever
blocks the NTRIP reader for longer than SEND_TIMEOUT.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Callable

from .transports import (DEFAULT_BAUD, Endpoint, SerialTransport, TcpTransport, Transport,
                         UdpOutTransport, UdpTransport, _bracket, _host_port)

SEND_TIMEOUT = 2.0               # a write that takes longer than this is dropped
RECONNECT_MIN, RECONNECT_MAX = 1.0, 15.0
UDP_PAYLOAD = 1400               # datagrams carry whole frames, up to this many bytes


@dataclass(frozen=True)
class SinkSpec:
    """A parsed --rtcm-out."""
    kind: str                    # "same", "serial", "tcp" or "udp"
    host: str = ""
    port: int = 0
    path: str = ""
    baud: int = 0

    @property
    def label(self) -> str:
        if self.kind == "same":
            return "same link"
        if self.kind == "serial":
            return self.path
        return f"{self.kind}://{_bracket(self.host)}:{self.port}"


def parse_sink(spec: str, protocol: str, source: Endpoint | None = None) -> SinkSpec:
    """Parse --rtcm-out.  `source` is the data endpoint, when there is one: naming the same
    tty as the source means "same" (a tty can only be opened once)."""
    if not spec:
        raise ValueError("--rtcm-out needs a destination")
    if spec == "same":
        return SinkSpec("same")
    if spec.startswith("file://"):
        raise ValueError("--rtcm-out cannot be a file")
    if spec.startswith("/"):
        path, sep, baud = spec.rpartition(":")
        if not (sep and baud.isdigit() and path):
            path, baud = spec, ""
        if (source is not None and source.kind == "serial"
                and os.path.realpath(path) == os.path.realpath(source.path)):
            return SinkSpec("same")
        return SinkSpec("serial", path=path, baud=int(baud) if baud else DEFAULT_BAUD[protocol])
    kind, rest = "tcp", spec
    if "://" in spec:
        scheme, rest = spec.split("://", 1)
        if scheme not in ("tcp", "udp"):
            raise ValueError(f"--rtcm-out: unknown scheme '{scheme}' (use tcp://, udp://, or a "
                             "/dev path)")
        kind = scheme
    host, port = _host_port(rest, spec)
    if not host:
        raise ValueError(f"--rtcm-out '{spec}' needs a host to send to")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"--rtcm-out '{spec}': give a port, 1 to 65535")
    return SinkSpec(kind, host=host, port=int(port))


class Sink:
    """Interface.  send() takes a batch of whole RTCM frames and never raises."""

    label = ""
    bytes_out = 0
    dropped = 0                  # bytes dropped because the sink was down
    state = "down"               # "up", "down"
    detail = ""                  # why it is down, for the message pane

    async def send(self, frames: list[bytes]) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    @property
    def dest_label(self) -> str:
        return self.label

    @property
    def short_label(self) -> str:
        """For the status bar, where room is short."""
        return self.label


def _batches(frames: list[bytes], limit: int) -> list[bytes]:
    """Frames joined into writes of at most `limit` bytes, never splitting a frame."""
    out: list[bytes] = []
    cur = b""
    for f in frames:
        if cur and len(cur) + len(f) > limit:
            out.append(cur)
            cur = b""
        cur += f
    if cur:
        out.append(cur)
    return out


class SameLinkSink(Sink):
    """Writes to the data link the app is reading.  `current` returns the app's current source
    transport, or None while it is reconnecting: the session recreates the transport on every
    retry, so a reference taken at start would go stale."""

    def __init__(self, current: Callable[[], Transport | None]) -> None:
        self._current = current
        self.label = "same link"
        self._dest = ""

    @property
    def dest_label(self) -> str:
        return self._dest or self.label

    @property
    def short_label(self) -> str:
        return "same link"

    async def send(self, frames: list[bytes]) -> None:
        transport = self._current()
        if transport is None:
            self.state, self.detail = "down", "the data link is not connected"
            self.dropped += sum(map(len, frames))
            return
        self._dest = f"{transport.label} (same link)"
        limit = UDP_PAYLOAD if isinstance(transport, UdpTransport) else 1 << 20
        try:
            for chunk in _batches(frames, limit):
                await asyncio.wait_for(transport.write(chunk), SEND_TIMEOUT)
                self.bytes_out += len(chunk)
            self.state, self.detail = "up", ""
        except (OSError, TimeoutError) as exc:
            self.state, self.detail = "down", str(exc) or type(exc).__name__
            self.dropped += sum(map(len, frames))


class TransportSink(Sink):
    """Writes to a separate interface of its own, opened on first use and reopened after a
    failure, at most once per backoff interval."""

    def __init__(self, spec: SinkSpec, loop_time: Callable[[], float] | None = None) -> None:
        self.spec = spec
        self.label = spec.label
        self._transport: Transport | None = None
        self._drain: asyncio.Task | None = None
        self._backoff = RECONNECT_MIN
        self._next_try = 0.0
        self._now = loop_time or (lambda: asyncio.get_running_loop().time())

    def _make(self) -> Transport:
        s = self.spec
        if s.kind == "serial":
            return SerialTransport(s.path, s.baud)
        if s.kind == "udp":
            return UdpOutTransport(s.host, s.port)
        return TcpTransport(s.host, s.port)

    async def send(self, frames: list[bytes]) -> None:
        size = sum(map(len, frames))
        if self._transport is None and not await self._open():
            self.dropped += size
            return
        t = self._transport
        assert t is not None
        limit = UDP_PAYLOAD if self.spec.kind == "udp" else 1 << 20
        try:
            for chunk in _batches(frames, limit):
                await asyncio.wait_for(t.write(chunk), SEND_TIMEOUT)
                self.bytes_out += len(chunk)
            self.state, self.detail = "up", ""
        except (OSError, TimeoutError) as exc:
            self.dropped += size
            await self._fail(str(exc) or type(exc).__name__)

    async def _open(self) -> bool:
        now = self._now()
        if now < self._next_try:
            return False
        t = self._make()
        try:
            await asyncio.wait_for(t.open(), SEND_TIMEOUT * 2)
        except (OSError, TimeoutError) as exc:
            await t.close()
            self.state, self.detail = "down", str(exc) or type(exc).__name__
            self._next_try = now + self._backoff
            self._backoff = min(self._backoff * 2, RECONNECT_MAX)
            return False
        self._transport = t
        self._backoff = RECONNECT_MIN
        self.state, self.detail = "up", ""
        if self.spec.kind == "tcp":              # read and discard, so a closed peer is noticed
            self._drain = asyncio.create_task(self._discard(t))
        return True

    async def _discard(self, t: Transport) -> None:
        try:
            while True:
                await t.read(5.0)
        except (OSError, asyncio.CancelledError):
            pass
        if self._transport is t:                  # the peer went away
            await self._fail("the connection was closed")

    async def _fail(self, why: str) -> None:
        t, self._transport = self._transport, None
        self.state, self.detail = "down", why
        self._next_try = self._now() + self._backoff
        self._backoff = min(self._backoff * 2, RECONNECT_MAX)
        if self._drain is not None and self._drain is not asyncio.current_task():
            self._drain.cancel()
        self._drain = None
        if t is not None:
            await t.close()

    async def close(self) -> None:
        if self._drain is not None:
            self._drain.cancel()
            self._drain = None
        t, self._transport = self._transport, None
        if t is not None:
            await t.close()
        self.state = "down"


def make_sink(spec: SinkSpec, current: Callable[[], Transport | None]) -> Sink:
    if spec.kind == "same":
        return SameLinkSink(current)
    return TransportSink(spec)
