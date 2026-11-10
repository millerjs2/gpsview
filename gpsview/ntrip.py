"""NTRIP client: pull RTCM corrections from a caster and hand them to a sink.

NTRIP (Networked Transport of RTCM via Internet Protocol) is HTTP with a twist.  A client asks
a caster for a mountpoint and the caster streams RTCM 3 frames back on the same connection:

    version 1   GET /MOUNT HTTP/1.0      answered by  "ICY 200 OK"  then raw data
    version 2   GET /MOUNT HTTP/1.1      answered by  "HTTP/1.1 200 OK", often with
                Ntrip-Version: Ntrip/2.0 "Transfer-Encoding: chunked", then data

A wrong mountpoint is answered with the caster's sourcetable ("SOURCETABLE 200 OK"), bad
credentials with 401, and a mountpoint that wants the rover's position (a VRS network) sends
nothing until it has had a GGA sentence from us.

The pieces:
    NtripConfig, parse_ntrip_url   what to connect to; credentials never appear in labels
    NtripClient                    one connection: request, reply, de-chunked payload, GGA
    fetch_sourcetable              list a caster's mountpoints
    NtripSession                   keeps a client connected (backoff), splits the stream into
                                   whole RTCM frames, forwards them to a corrections.Sink, and
                                   keeps the numbers the status line shows

Nothing in this module knows about Textual or about any receiver.
"""
from __future__ import annotations

import asyncio
import base64
import os
import ssl
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import unquote, urlsplit

from . import __version__
from .corrections import Sink, parse_sink
from .rtcm import RtcmFramer

DEFAULT_PORT = 2101
USER_AGENT = f"NTRIP gpsview/{__version__}"
CONNECT_TIMEOUT = 10.0
ICY_GRACE = 0.5                  # how long a bare "ICY 200 OK" is waited on for more
IDLE_TIMEOUT = 30.0              # no bytes from the caster for this long: reconnect
POLL = 1.0                       # read granularity (GGA schedule, idle check)
BACKOFF_MIN, BACKOFF_MAX = 1.0, 60.0
MAX_SOURCETABLE = 2_000_000


# ----------------------------------------------------------------- configuration
@dataclass(frozen=True)
class NtripConfig:
    host: str
    port: int = DEFAULT_PORT
    mountpoint: str = ""
    user: str = ""
    password: str = field(default="", repr=False)     # never printed
    tls: bool = False
    version: int = 2
    gga_interval: float = 0.0                         # seconds between GGA; 0 = never send

    @property
    def label(self) -> str:
        """For humans and logs.  No credentials."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        scheme = "ntrips" if self.tls else "ntrip"
        return f"{scheme}://{host}:{self.port}/{self.mountpoint}"

    @property
    def auth_header(self) -> str:
        if not self.user:
            return ""
        raw = f"{self.user}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


def parse_ntrip_url(spec: str, *, user: str | None = None, password: str | None = None,
                    environ: Mapping[str, str] | None = None,
                    need_mountpoint: bool = True) -> NtripConfig:
    """`[ntrip://|ntrips://][user[:password]@]host[:port]/mountpoint`.

    Credentials come from the URL, else from `user`/`password`, else from the NTRIP_USER and
    NTRIP_PASSWORD environment variables.  A password on a command line shows in `ps` and in
    shell history, so the environment is the better place for it.  ntrips:// uses TLS.
    """
    env = os.environ if environ is None else environ
    if not spec:
        raise ValueError("--ntrip needs a caster, as host[:port]/mountpoint")
    text = spec if "://" in spec else "ntrip://" + spec
    parts = urlsplit(text)
    if parts.scheme not in ("ntrip", "ntrips", "http", "https"):
        raise ValueError(f"--ntrip: unknown scheme '{parts.scheme}' (use ntrip:// or ntrips://)")
    try:
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise ValueError(f"--ntrip '{_scrub(spec)}': {exc}") from None
    if not host:
        raise ValueError("--ntrip needs a caster host")
    mount = unquote(parts.path.lstrip("/"))
    if need_mountpoint and not mount:
        raise ValueError("--ntrip needs a mountpoint, as host[:port]/MOUNTPOINT "
                         "(--ntrip-list shows what a caster offers)")
    # Each of user and password comes from the URL, else the option, else the environment, so
    # "me@host/MOUNT" takes its password from NTRIP_PASSWORD.
    if parts.username:
        who = unquote(parts.username)
    else:
        who = user if user is not None else env.get("NTRIP_USER", "")
    if parts.password is not None:
        secret = unquote(parts.password)
    else:
        secret = password if password is not None else env.get("NTRIP_PASSWORD", "")
    return NtripConfig(host=host, port=port or DEFAULT_PORT, mountpoint=mount, user=who,
                       password=secret, tls=parts.scheme in ("ntrips", "https"))


def _scrub(spec: str) -> str:
    """The spec with any user:password@ removed, for error messages."""
    if "@" in spec:
        scheme, sep, rest = spec.partition("://")
        return (scheme + sep if sep else "") + rest.rsplit("@", 1)[1]
    return spec


def make_setup(url: str, rtcm_out: str | None, *, endpoint, protocol: str,
               user: str | None = None, password: str | None = None, version: int = 2,
               gga: float = 0.0):
    """Everything --ntrip and the RTK dialog need, checked in one place: the NTRIP config and
    where the corrections go.  Raises ValueError with a message for the user.

    `endpoint` is the data source (None for gpsd) and `protocol` its name: "same" needs a
    link gpsview owns, which gpsd and files are not."""
    from dataclasses import replace
    cfg = parse_ntrip_url(url, user=user, password=password)
    if gga < 0:
        raise ValueError("--ntrip-gga must be 0 or more")
    cfg = replace(cfg, version=version, gga_interval=gga)
    sink = parse_sink(rtcm_out or "same", protocol or "gpsd", endpoint)
    if sink.kind == "same":
        if endpoint is None:
            raise ValueError("a gpsd connection cannot take corrections: gpsd owns the receiver. "
                             "Give --rtcm-out DEST (a tty or host:port the receiver listens on); "
                             "gpsd can also fetch NTRIP itself, with  gpsd ntrip://...")
        if endpoint.kind == "file":
            raise ValueError("a file cannot take corrections: give --rtcm-out DEST")
    return cfg, sink


# ----------------------------------------------------------------- errors
class NtripError(Exception):
    """Something went wrong talking to the caster.  `fatal` means retrying will not help
    (bad credentials, no such mountpoint): the user has to change something."""

    fatal = False


class NtripAuthError(NtripError):
    fatal = True


class NtripMountpointError(NtripError):
    fatal = True


class NtripFatalError(NtripError):
    fatal = True


# ----------------------------------------------------------------- wire format
def build_request(cfg: NtripConfig, mountpoint: str | None = None) -> bytes:
    mount = cfg.mountpoint if mountpoint is None else mountpoint
    host = f"[{cfg.host}]" if ":" in cfg.host else cfg.host
    lines = [f"GET /{mount} HTTP/{'1.1' if cfg.version >= 2 else '1.0'}"]
    if cfg.version >= 2:
        lines += [f"Host: {host}:{cfg.port}", "Ntrip-Version: Ntrip/2.0"]
    lines.append(f"User-Agent: {USER_AGENT}")
    if cfg.auth_header:
        lines.append(f"Authorization: {cfg.auth_header}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", "replace")


@dataclass
class Reply:
    status_line: str
    headers: dict[str, str]
    rest: bytes                  # payload bytes that came in the same read as the headers


def parse_reply(buf: bytes) -> Reply | None:
    """Split a caster's reply into status line, headers and the payload that follows.

    None when more bytes are needed.  Version 1 casters answer "ICY 200 OK" and then may send
    the data at once, without the blank line that ends HTTP headers: after an ICY status line
    a blank line or header lines are consumed if present, anything else is payload.
    """
    end = buf.find(b"\n")
    if end < 0:
        return None if len(buf) < 4096 else Reply(buf.decode("latin-1"), {}, b"")
    status = buf[:end].decode("latin-1").strip()
    rest = buf[end + 1:]
    if status.startswith("ICY "):
        if rest.startswith(b"\r\n") or rest.startswith(b"\n"):
            return Reply(status, {}, rest[2:] if rest.startswith(b"\r\n") else rest[1:])
        if not _looks_like_header(rest):
            if len(rest) < 2 and rest[:1] in (b"\r", b"\n", b""):
                return None
            return Reply(status, {}, rest)
    elif status.startswith("SOURCETABLE ") or status.startswith("ERROR"):
        return Reply(status, {}, rest)               # no headers worth waiting for
    head_end = rest.find(b"\r\n\r\n")
    sep_len = 4
    if head_end < 0:
        head_end, sep_len = rest.find(b"\n\n"), 2
    if head_end < 0:
        return None if len(buf) < 16384 else Reply(status, {}, b"")
    headers: dict[str, str] = {}
    for line in rest[:head_end].decode("latin-1").splitlines():
        key, _, value = line.partition(":")
        if value:
            headers[key.strip().lower()] = value.strip()
    return Reply(status, headers, rest[head_end + sep_len:])


def _looks_like_header(data: bytes) -> bool:
    line = data.split(b"\n", 1)[0]
    key, colon, _ = line.partition(b":")
    return bool(colon) and key.replace(b"-", b"").isalnum() and len(key) < 40


class ChunkedDecoder:
    """Removes HTTP chunked transfer encoding.  Without it, the chunk-size lines are
    injected into the RTCM stream and every frame near one fails its CRC."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._need = -1               # bytes left in the current chunk; -1 = expecting a size line
        self._skip_crlf = False
        self.finished = False

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        buf = self._buf
        while buf and not self.finished:
            if self._skip_crlf:
                if len(buf) < 2:
                    break
                del buf[:2]
                self._skip_crlf = False
                continue
            if self._need < 0:
                eol = buf.find(b"\r\n")
                if eol < 0:
                    if len(buf) > 64:
                        raise NtripError("the caster's chunked encoding is malformed")
                    break
                size_text = bytes(buf[:eol]).split(b";", 1)[0].strip()
                del buf[:eol + 2]
                try:
                    self._need = int(size_text, 16)
                except ValueError:
                    raise NtripError("the caster's chunked encoding is malformed") from None
                if self._need == 0:
                    self.finished = True
                continue
            take = min(self._need, len(buf))
            out += buf[:take]
            del buf[:take]
            self._need -= take
            if self._need == 0:
                self._need = -1
                self._skip_crlf = True
        return bytes(out)


def check_reply(reply: Reply, cfg: NtripConfig, *, listing: bool = False) -> bool:
    """Raise the right NtripError for a reply that is not a stream.  Returns True when the
    payload is chunked.  `listing` says a sourcetable is what was asked for."""
    status = reply.status_line
    upper = status.upper()
    where = cfg.label
    if upper.startswith("ICY 200"):
        return False
    if upper.startswith("SOURCETABLE 200"):
        if listing:
            return False
        raise NtripMountpointError(
            f"{where}: the caster has no mountpoint '{cfg.mountpoint}' (it sent its "
            "sourcetable); --ntrip-list shows the names")
    if upper.startswith("ERROR"):
        if "PASSWORD" in upper or "UNAUTHORIZED" in upper:
            raise NtripAuthError(f"{where}: the caster rejected the credentials ({status})")
        raise NtripMountpointError(f"{where}: {status}")
    if upper.startswith("HTTP/"):
        fields = status.split(None, 2)
        code = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
        reason = fields[2] if len(fields) > 2 else ""
        ctype = reply.headers.get("content-type", "").lower()
        if code == 200:
            if "sourcetable" in ctype and not listing:
                raise NtripMountpointError(
                    f"{where}: the caster has no mountpoint '{cfg.mountpoint}' (it sent its "
                    "sourcetable); --ntrip-list shows the names")
            return "chunked" in reply.headers.get("transfer-encoding", "").lower()
        if code == 401:
            raise NtripAuthError(f"{where}: the caster needs a username and password, or "
                                 "rejected the ones given (401)" if not cfg.user else
                                 f"{where}: the caster rejected the username or password (401)")
        if code == 403:
            raise NtripAuthError(f"{where}: this user may not use this mountpoint (403)")
        if code == 404:
            raise NtripMountpointError(f"{where}: no such mountpoint (404)")
        if 300 <= code < 400:
            raise NtripFatalError(f"{where}: redirected to {reply.headers.get('location', '?')}"
                                  " (not followed; use that address)")
        if code in (400, 405, 505) and cfg.version >= 2:
            raise NtripFatalError(f"{where}: the caster refused the request ({code} {reason}); "
                                  "it may be NTRIP version 1 only: try --ntrip-version 1")
        err = NtripError(f"{where}: HTTP {code} {reason}".rstrip())
        err.fatal = 400 <= code < 500 and code not in (408, 429)
        raise err
    raise NtripError(f"{where}: unexpected reply '{status[:60]}'")


# ----------------------------------------------------------------- one connection
class NtripClient:
    def __init__(self, cfg: NtripConfig, *, connect_timeout: float = CONNECT_TIMEOUT) -> None:
        self.cfg = cfg
        self._timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._chunks: ChunkedDecoder | None = None
        self._pending = b""

    async def connect(self, *, listing: bool = False) -> bytes:
        """Open the connection and read the caster's reply.  Returns the payload bytes that
        arrived with the reply (de-chunked), which may be empty."""
        cfg = self.cfg
        ctx = ssl.create_default_context() if cfg.tls else None
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(cfg.host, cfg.port, ssl=ctx,
                                        server_hostname=cfg.host if ctx else None),
                self._timeout)
        except TimeoutError:
            raise ConnectionError(f"{cfg.label}: no answer within {self._timeout:.0f} s") from None
        except ssl.SSLError as exc:
            raise NtripFatalError(f"{cfg.label}: TLS failed: {exc.reason or exc}") from None
        self._writer.write(build_request(cfg, "" if listing else None))
        await self._writer.drain()
        buf = b""
        while True:
            # A version 1 caster that serves a VRS mountpoint says "ICY 200 OK" and then waits
            # for our GGA: if nothing follows the status line for a moment, that is the reply.
            icy = buf.startswith(b"ICY 200") and b"\n" in buf
            try:
                data = await asyncio.wait_for(self._reader.read(8192),
                                              ICY_GRACE if icy else self._timeout)
            except TimeoutError:
                if icy:
                    line, _, rest = buf.partition(b"\n")
                    reply = Reply(line.decode("latin-1").strip(), {}, rest.lstrip(b"\r\n"))
                    break
                raise ConnectionError(f"{cfg.label}: the caster did not answer") from None
            if not data:
                if buf:
                    reply = parse_reply(buf + b"\r\n\r\n") or Reply(buf.decode("latin-1"), {}, b"")
                    break
                raise ConnectionError(f"{cfg.label}: the caster closed the connection")
            buf += data
            reply = parse_reply(buf)
            if reply is not None:
                break
        chunked = check_reply(reply, cfg, listing=listing)
        self._chunks = ChunkedDecoder() if chunked else None
        return self._payload(reply.rest)

    def _payload(self, data: bytes) -> bytes:
        return self._chunks.feed(data) if self._chunks is not None else data

    async def read(self, timeout: float) -> bytes | None:
        """Payload bytes, or None if nothing arrived within `timeout`.  Raises
        ConnectionError when the stream ends."""
        assert self._reader is not None
        while True:
            try:
                data = await asyncio.wait_for(self._reader.read(65536), timeout)
            except TimeoutError:
                return None
            if not data:
                raise ConnectionError(f"{self.cfg.label}: the caster closed the stream")
            payload = self._payload(data)
            if self._chunks is not None and self._chunks.finished:
                if payload:
                    return payload
                raise ConnectionError(f"{self.cfg.label}: the caster ended the stream")
            if payload:
                return payload

    async def send(self, data: bytes) -> None:
        """Send a GGA sentence (the only thing a client sends after the request)."""
        assert self._writer is not None
        self._writer.write(data)
        await asyncio.wait_for(self._writer.drain(), self._timeout)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), 2.0)
            except (OSError, TimeoutError, ssl.SSLError):
                pass
            self._writer = self._reader = None


# ----------------------------------------------------------------- sourcetable
@dataclass(frozen=True)
class Stream:
    mountpoint: str
    identifier: str = ""
    format: str = ""
    format_details: str = ""
    carrier: str = ""
    nav_system: str = ""
    network: str = ""
    country: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    nmea: bool = False            # the stream needs the rover's GGA (a VRS network)
    authentication: str = ""
    bitrate: int = 0


def parse_sourcetable(text: str) -> list[Stream]:
    """The STR lines of a sourcetable.  Short or odd lines are skipped, not fatal."""
    out: list[Stream] = []
    for line in text.splitlines():
        if not line.startswith("STR;"):
            continue
        f = line.split(";")
        if len(f) < 12 or not f[1]:           # too short to say what the stream is
            continue
        f += [""] * (19 - len(f))

        def num(text: str, kind=float):
            try:
                return kind(text)
            except ValueError:
                return kind(0)
        out.append(Stream(f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], num(f[9]), num(f[10]),
                          f[11].strip() == "1", f[15], num(f[17], int)))
    return out


async def fetch_sourcetable(cfg: NtripConfig, timeout: float = CONNECT_TIMEOUT) -> list[Stream]:
    """Ask the caster for its mountpoints."""
    client = NtripClient(cfg, connect_timeout=timeout)
    try:
        text = (await client.connect(listing=True)).decode("latin-1")
        deadline = time.monotonic() + timeout
        while "ENDSOURCETABLE" not in text and len(text) < MAX_SOURCETABLE:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                data = await client.read(left)
            except ConnectionError:
                break
            if data:
                text += data.decode("latin-1")
        return parse_sourcetable(text)
    finally:
        await client.close()


# ----------------------------------------------------------------- the session
def _size(n: int) -> str:
    return f"{n} B" if n < 1000 else f"{n / 1000:.1f} kB" if n < 1_000_000 else f"{n / 1e6:.1f} MB"


class NtripSession:
    """Keeps one caster connection alive and forwards what it sends.

    notify(text, style) reports state changes (the app shows them in the message pane).
    gga() returns a GGA sentence for the current position, or None when there is none.
    """

    def __init__(self, cfg: NtripConfig, sink: Sink, *,
                 gga: Callable[[], bytes | None] | None = None,
                 notify: Callable[[str, str], None] | None = None,
                 client_factory: Callable[[NtripConfig], NtripClient] = NtripClient,
                 idle_timeout: float = IDLE_TIMEOUT,
                 backoff: tuple[float, float] = (BACKOFF_MIN, BACKOFF_MAX)) -> None:
        self.cfg = cfg
        self.sink = sink
        self._gga = gga
        self._notify = notify or (lambda text, style: None)
        self._factory = client_factory
        self._idle = idle_timeout
        self._backoff = backoff
        self.state = "connecting"      # connecting, streaming, waiting, failed
        self.error = ""
        self.bytes_in = 0
        self.frames_in = 0
        self.types: Counter[int] = Counter()
        self.last_frame: float | None = None      # time.monotonic() of the last whole frame
        self.connects = 0
        self.gga_sent = 0
        self.framer = RtcmFramer()
        self._sink_state = ""

    # status ---------------------------------------------------------------
    def age(self, now: float | None = None) -> float | None:
        """Seconds since the last whole RTCM frame arrived (the age of the corrections)."""
        if self.last_frame is None:
            return None
        return (time.monotonic() if now is None else now) - self.last_frame

    def status_text(self, receiver: str = "", width: int = 0) -> str:
        """One line for the status bar, made to fit `width` columns (0 = no limit).

        `receiver` is the receiver's own report on the corrections, if it has one.  When the
        line is too long the least useful parts go first: the message types, the bytes sent,
        the bytes received.  The state, the destination, the receiver's report and the age
        of the corrections stay.
        """
        cfg, sink = self.cfg, self.sink
        head = f"ntrip {cfg.mountpoint or cfg.host} {self.state}"
        if self.state == "failed" and self.error:
            head += f": {self.error}"
        age = self.age()
        # (text, priority): a higher number is dropped sooner
        parts = [(head, 0), (f"-> {sink.short_label} {sink.state}", 0)]
        if receiver:
            parts.append((receiver, 1))
        if self.frames_in:
            parts.append((f"age {age:.0f} s" if age is not None else "", 2))
            parts.append((f"rx {_size(self.bytes_in)}", 4))
            parts.append((" ".join(str(t) for t, _ in self.types.most_common(6)), 5))
        if sink.dropped:
            parts.append((f"dropped {_size(sink.dropped)}", 3))
        elif sink.bytes_out:
            parts.append((f"out {_size(sink.bytes_out)}", 5))
        while True:
            text = "  ".join(t for t, _ in parts if t)
            if not width or len(text) <= width:
                return text
            droppable = [p for p in parts if p[1] > 0 and p[0]]
            if not droppable:
                return text[:max(width - 1, 1)] + "…"
            parts.remove(max(droppable, key=lambda p: p[1]))

    # the loop -------------------------------------------------------------
    def _say(self, text: str, style: str = "") -> None:
        self._notify(text, style)

    async def run(self) -> None:
        """Connect, stream, and reconnect until cancelled or the caster refuses us for good."""
        delay = self._backoff[0]
        try:
            while True:
                client = self._factory(self.cfg)
                self.state = "connecting"
                try:
                    first = await client.connect()
                    self.connects += 1
                    self.state, self.error = "streaming", ""
                    self._say(f"NTRIP: streaming {self.cfg.label}", "green")
                    delay = self._backoff[0]
                    await self._forward(first)
                    await self._stream(client)
                except NtripError as exc:
                    self.error = str(exc)
                    if exc.fatal:
                        self.state = "failed"
                        self._say(f"NTRIP: {exc}. Not retrying.", "red")
                        return
                    self._say(f"NTRIP: {exc}", "red")
                except (OSError, ssl.SSLError) as exc:
                    self.error = str(exc) or type(exc).__name__
                    self._say(f"NTRIP: {self.cfg.label}: {self.error}", "red")
                finally:
                    await client.close()
                self.state = "waiting"
                self._say(f"NTRIP: reconnecting in {delay:.0f} s", "yellow")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._backoff[1])
        finally:
            await self.sink.close()

    async def _stream(self, client: NtripClient) -> None:
        loop = asyncio.get_running_loop()
        last_data = loop.time()
        next_gga = loop.time()
        interval = self.cfg.gga_interval
        while True:
            now = loop.time()
            if interval > 0 and self._gga is not None and now >= next_gga:
                sentence = self._gga()
                if sentence is not None:
                    await client.send(sentence)
                    self.gga_sent += 1
                    next_gga = now + interval
                else:
                    next_gga = now + 1.0              # no position yet: ask again soon
            data = await client.read(POLL)
            if data is None:
                if loop.time() - last_data >= self._idle:
                    raise ConnectionError(f"no data from the caster for {self._idle:.0f} s")
                continue
            last_data = loop.time()
            await self._forward(data)

    async def _forward(self, data: bytes) -> None:
        """Count the bytes, split off whole frames, send them on.  Bytes that are not part of a
        good RTCM frame are not forwarded."""
        if not data:
            return
        self.bytes_in += len(data)
        frames = self.framer.feed(data)
        if not frames:
            return
        for f in frames:
            self.types[f.type] += 1
        self.frames_in += len(frames)
        self.last_frame = time.monotonic()
        await self.sink.send([f.data for f in frames])
        if self.sink.state != self._sink_state:
            self._sink_state = self.sink.state
            if self.sink.state == "up":
                self._say(f"NTRIP: sending corrections to {self.sink.dest_label}", "green")
            else:
                self._say(f"NTRIP: cannot send corrections to {self.sink.dest_label}: "
                          f"{self.sink.detail}. Dropping them until it works.", "red")
