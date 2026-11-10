"""Byte transports for the three ways a receiver's data can reach us: TCP, UDP and a serial tty.

A transport only moves bytes.  read() waits up to a timeout and returns None when nothing
arrived, so the app can check for a stalled link.  Nothing here writes to the receiver:
write() exists only for gpsd's ?WATCH request.

Endpoint grammar (for -n and -sbp):
    /dev/ttyUSB0            serial, default baud for the protocol
    /dev/ttyUSB0:115200     serial at a given baud
    [tcp://]host:port       TCP client;  [ipv6]:port  for IPv6
    udp://[host]:port       UDP: bind to host (udp://:port binds all interfaces) and read
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

POLL_CHUNK = 65536
UDP_QUEUE = 4096                       # datagrams kept while the app is busy
DEFAULT_BAUD = {"nmea": 9600, "sbp": 115200}   # what receivers ship with


class Transport:
    """Interface: open(), read(timeout), write(data), close()."""

    label = ""

    async def open(self) -> None:
        raise NotImplementedError

    async def read(self, timeout: float) -> bytes | None:
        """Bytes received, or None if nothing arrived within timeout.

        Raises ConnectionError (or OSError) when the link is gone.
        """
        raise NotImplementedError

    async def write(self, data: bytes) -> None:
        raise OSError(f"{self.label} is read-only for this client")

    async def close(self) -> None:
        pass


class TcpTransport(Transport):
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, int(port)
        self.label = f"tcp://{_bracket(host)}:{self.port}"
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def read(self, timeout: float) -> bytes | None:
        assert self._reader is not None
        try:
            data = await asyncio.wait_for(self._reader.read(POLL_CHUNK), timeout)
        except TimeoutError:
            return None
        if not data:
            raise ConnectionError(f"{self.label} closed the connection")
        return data

    async def write(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None


class _DatagramQueue(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:          # the app is behind: drop, as a UDP stack would
            pass


class UdpTransport(Transport):
    def __init__(self, host: str, port: int) -> None:
        self.host = host or "0.0.0.0"
        self.port = int(port)
        self.label = f"udp://{_bracket(host)}:{self.port}"
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=UDP_QUEUE)
        self._transport: asyncio.DatagramTransport | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DatagramQueue(self._queue), local_addr=(self.host, self.port))

    async def read(self, timeout: float) -> bytes | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class SerialTransport(Transport):
    def __init__(self, path: str, baud: int) -> None:
        self.path, self.baud = path, int(baud)
        self.label = path
        self._ser = None

    async def open(self) -> None:
        import serial                       # pyserial; imported here so TCP/UDP work without it
        self._ser = await asyncio.to_thread(serial.Serial, self.path, self.baud, timeout=1.0)

    async def read(self, timeout: float) -> bytes | None:
        data = await asyncio.to_thread(self._read_blocking, timeout)
        return data or None

    def _read_blocking(self, timeout: float) -> bytes:
        ser = self._ser
        ser.timeout = timeout
        first = ser.read(1)                  # waits up to timeout for the first byte
        if not first:
            return b""
        return first + ser.read(ser.in_waiting)

    async def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except OSError:
                pass
            self._ser = None


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


# ----------------------------------------------------------------- endpoints
@dataclass(frozen=True)
class Endpoint:
    kind: str                 # "tcp", "udp" or "serial"
    host: str = ""
    port: int = 0
    path: str = ""
    baud: int = 0

    @property
    def label(self) -> str:
        if self.kind == "serial":
            return self.path
        return f"{self.kind}://{_bracket(self.host)}:{self.port}"

    @property
    def device(self) -> str:
        """The device name for the status bar: the tty for a serial port, else none."""
        return self.path if self.kind == "serial" else ""

    def make_transport(self) -> Transport:
        if self.kind == "serial":
            return SerialTransport(self.path, self.baud)
        if self.kind == "udp":
            return UdpTransport(self.host, self.port)
        return TcpTransport(self.host, self.port)


def parse_endpoint(spec: str, protocol: str) -> Endpoint:
    """Parse an endpoint spec; raises ValueError with a message the CLI can show."""
    if not spec:
        raise ValueError("empty source")
    if spec.startswith("/"):
        path, sep, baud = spec.rpartition(":")
        if sep and baud.isdigit() and path:
            return Endpoint("serial", path=path, baud=int(baud))
        return Endpoint("serial", path=spec, baud=DEFAULT_BAUD[protocol])

    kind = "tcp"
    rest = spec
    if "://" in spec:
        scheme, rest = spec.split("://", 1)
        if scheme not in ("tcp", "udp"):
            raise ValueError(f"unknown scheme '{scheme}' (use tcp:// or udp://, or a /dev path)")
        kind = scheme
    host, port = _host_port(rest, spec)
    if not port:
        raise ValueError(f"'{spec}' needs a port, as host:port")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"'{spec}': port must be 1 to 65535")
    if kind == "tcp" and not host:
        raise ValueError(f"'{spec}' needs a host for TCP")
    return Endpoint(kind, host=host, port=int(port))


def _host_port(rest: str, spec: str) -> tuple[str, str]:
    if rest.startswith("["):
        close = rest.find("]")
        if close < 0:
            raise ValueError(f"'{spec}': missing ']' in IPv6 address")
        host = rest[1:close]
        tail = rest[close + 1:]
        if tail and not tail.startswith(":"):
            raise ValueError(f"'{spec}': expected ':port' after ']'")
        return host, tail[1:]
    if rest.count(":") > 1:
        raise ValueError(f"'{spec}': write an IPv6 address in brackets, as [addr]:port")
    host, _, port = rest.partition(":")
    return host, port
