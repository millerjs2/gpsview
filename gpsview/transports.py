"""Byte transports for the ways a receiver's data can reach us: TCP, UDP, a serial tty, or a
file.

A transport moves bytes.  read() waits up to a timeout and returns None when nothing
arrived, so the app can check for a stalled link.  By default nothing writes to the
receiver: write() is used for gpsd's ?WATCH request and, only when the user asks for NTRIP
(see ntrip.py and corrections.py), for RTCM corrections.

Endpoint grammar (for -n, -sbp and -ubx):
    /dev/ttyUSB0            serial, default baud for the protocol
    /dev/ttyUSB0:115200     serial at a given baud
    [tcp://]host:port       TCP client;  [ipv6]:port  for IPv6
    udp://[host]:port       UDP: bind to host (udp://:port binds all interfaces) and read
    file://PATH             a file, replayed at roughly a serial port's speed
    /path/to/file           also a file, when PATH is a regular file (not a device)
    ./file, ../file, ~/file also a file, when they exist as a regular file
"""
from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass

POLL_CHUNK = 65536
UDP_QUEUE = 4096                       # datagrams kept while the app is busy
# What receivers ship with.  u-blox: M8 and earlier UART1 is 9600; ZED-F9P is 38400.  UBX
# is set to the F9 default; use /dev/ttyX:9600 for an M8.  USB ports ignore the baud rate.
DEFAULT_BAUD = {"nmea": 9600, "sbp": 115200, "ubx": 38400, "gpsd": 115200}
FILE_RATE = 11520                      # bytes per second when replaying a file (115200 baud, 8N1)
FILE_TICK = 0.05                       # replay granularity, seconds


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
        raise OSError(f"{self.label} cannot be written to")

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
    def __init__(self, queue: asyncio.Queue, on_peer=None) -> None:
        self._queue = queue
        self._on_peer = on_peer

    def datagram_received(self, data: bytes, addr) -> None:
        if self._on_peer is not None:
            self._on_peer(addr)
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
        self.peer = None               # (host, port) of the last datagram's sender

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self.peer = None
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DatagramQueue(self._queue, self._set_peer),
            local_addr=(self.host, self.port))

    def _set_peer(self, addr) -> None:
        self.peer = addr

    async def write(self, data: bytes) -> None:
        """Reply to whoever last sent us a datagram (a receiver that streams to us over UDP
        is assumed to listen on the address it sends from).  Used for RTCM corrections only."""
        if self._transport is None:
            raise ConnectionError(f"{self.label} is not open")
        if self.peer is None:
            raise OSError(f"{self.label}: no datagram has arrived yet, so there is no "
                          "address to send to")
        self._transport.sendto(data, self.peer)

    async def read(self, timeout: float) -> bytes | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class EndOfFile(ConnectionError):
    """A replayed file ran out.  Not a failed link: the app says so and offers a replay."""

    def __init__(self, path: str, size: int) -> None:
        super().__init__(f"end of {path} ({size} bytes)")
        self.path, self.size = path, size


class FileTransport(Transport):
    """Replays a file.  Reads are paced at `rate` bytes per second (0 = as fast as possible),
    so a recording plays back as it would have arrived, not all at once.  At the end of the
    file read() raises EndOfFile.  The file is never written."""

    def __init__(self, path: str, rate: float = FILE_RATE) -> None:
        self.path = path
        self.rate = float(rate)
        self.label = f"file://{path}"
        self.position = 0
        self._fh = None
        self._size = 0

    async def open(self) -> None:
        self._fh = await asyncio.to_thread(open, self.path, "rb")
        self._size = os.fstat(self._fh.fileno()).st_size
        self.position = 0

    async def read(self, timeout: float) -> bytes | None:
        assert self._fh is not None
        if self.rate > 0:
            want = max(1, int(self.rate * FILE_TICK))
            await asyncio.sleep(FILE_TICK)
        else:
            want = POLL_CHUNK
        data = await asyncio.to_thread(self._fh.read, want)
        if not data:
            raise EndOfFile(self.path, self.position)
        self.position += len(data)
        return data

    async def write(self, data: bytes) -> None:
        raise OSError("a file cannot take corrections")

    async def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class UdpOutTransport(Transport):
    """Sends datagrams to a fixed address.  Output only: read() is never used."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, int(port)
        self.label = f"udp://{_bracket(host)}:{self.port}"
        self._transport: asyncio.DatagramTransport | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=(self.host, self.port))

    async def read(self, timeout: float) -> bytes | None:
        await asyncio.sleep(timeout)
        return None

    async def write(self, data: bytes) -> None:
        if self._transport is None:
            raise ConnectionError(f"{self.label} is not open")
        self._transport.sendto(data)

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class SerialTransport(Transport):
    def __init__(self, path: str, baud: int) -> None:
        self.path, self.baud = path, int(baud)
        self.label = path
        self._ser = None

    async def write(self, data: bytes) -> None:
        ser = self._ser
        if ser is None:
            raise ConnectionError(f"{self.path} is not open")
        await asyncio.to_thread(ser.write, data)

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
    kind: str                 # "tcp", "udp", "serial" or "file"
    host: str = ""
    port: int = 0
    path: str = ""
    baud: int = 0
    rate: float = FILE_RATE   # file replay speed, bytes per second (0 = unpaced)

    @property
    def label(self) -> str:
        if self.kind == "serial":
            return self.path
        if self.kind == "file":
            return f"file://{self.path}"
        return f"{self.kind}://{_bracket(self.host)}:{self.port}"

    @property
    def device(self) -> str:
        """The device name for the status bar: the tty for a serial port, else none."""
        return self.path if self.kind == "serial" else ""

    def make_transport(self) -> Transport:
        if self.kind == "serial":
            return SerialTransport(self.path, self.baud)
        if self.kind == "file":
            return FileTransport(self.path, self.rate)
        if self.kind == "udp":
            return UdpTransport(self.host, self.port)
        return TcpTransport(self.host, self.port)


def parse_endpoint(spec: str, protocol: str) -> Endpoint:
    """Parse an endpoint spec; raises ValueError with a message the CLI can show."""
    if not spec:
        raise ValueError("empty source")
    if spec.startswith("file://"):
        path = os.path.expanduser(spec[len("file://"):])
        if not path:
            raise ValueError(f"'{spec}' needs a path, as file:///path/to/file")
        _check_file(path, spec)
        return Endpoint("file", path=path)
    if spec.startswith(("/", "./", "../", "~")):
        local = os.path.expanduser(spec)
        if os.path.isfile(local):               # a regular file, not a tty or a socket
            return Endpoint("file", path=local)
        if not spec.startswith(("/", "~")):
            raise ValueError(f"'{spec}': no such file (a tty must be a /dev path)")
        path, sep, baud = local.rpartition(":")
        if sep and baud.isdigit() and path:
            return Endpoint("serial", path=path, baud=int(baud))
        return Endpoint("serial", path=local, baud=DEFAULT_BAUD[protocol])

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


def _check_file(path: str, spec: str) -> None:
    if not os.path.exists(path):
        raise ValueError(f"'{spec}': no such file")
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise ValueError(f"'{spec}': not a regular file (give a tty as /dev/name)")


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
