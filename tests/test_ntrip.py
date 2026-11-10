"""NTRIP: the client against a fake caster (versions 1 and 2, chunked, auth, sourcetable, VRS),
and the session that forwards whole RTCM frames to a sink."""
import asyncio

import pytest

from gpsview import rtcm
from gpsview.corrections import Sink
from gpsview.ntrip import (ChunkedDecoder, NtripAuthError, NtripClient, NtripConfig,
                           NtripError, NtripFatalError, NtripMountpointError, NtripSession,
                           Reply, build_request, check_reply, fetch_sourcetable,
                           parse_ntrip_url, parse_reply, parse_sourcetable)
from tools.fake_ntrip import SOURCETABLE, FakeCaster, frames_for


def run(coro):
    return asyncio.run(coro)


class CaptureSink(Sink):
    """Records what it is given, as a receiver would see it."""
    label = "capture"

    def __init__(self):
        self.batches: list[list[bytes]] = []
        self.state = "up"

    async def send(self, frames):
        self.batches.append(list(frames))
        self.bytes_out += sum(map(len, frames))

    @property
    def data(self) -> bytes:
        return b"".join(f for batch in self.batches for f in batch)


def cfg_for(port, mount="RTCM", **kw):
    return NtripConfig(host="127.0.0.1", port=port, mountpoint=mount, **kw)


async def until(cond, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


# ----------------------------------------------------------------- configuration
def test_url_forms_and_credentials():
    c = parse_ntrip_url("ntrip://me:p%40ss:w@caster.example.com:2102/MOUNT_1", environ={})
    assert (c.host, c.port, c.mountpoint, c.user, c.password, c.tls) == (
        "caster.example.com", 2102, "MOUNT_1", "me", "p@ss:w", False)
    c = parse_ntrip_url("caster.example.com/AB", environ={})
    assert (c.port, c.tls, c.user) == (2101, False, "")
    assert parse_ntrip_url("ntrips://h/M", environ={}).tls is True
    assert parse_ntrip_url("[::1]:2101/A", environ={}).host == "::1"
    assert parse_ntrip_url("h/M", user="u", password="p", environ={}).auth_header.startswith("Basic ")


def test_credential_precedence_url_then_options_then_environment():
    env = {"NTRIP_USER": "envu", "NTRIP_PASSWORD": "envp"}
    assert parse_ntrip_url("h/M", environ=env).user == "envu"
    assert parse_ntrip_url("h/M", environ=env).password == "envp"
    assert parse_ntrip_url("h/M", user="opt", environ=env).user == "opt"
    assert parse_ntrip_url("h/M", user="opt", environ=env).password == "envp"
    c = parse_ntrip_url("urlu:urlp@h/M", user="opt", password="x", environ=env)
    assert (c.user, c.password) == ("urlu", "urlp")
    # the documented form: a user in the URL, the password from the environment
    c = parse_ntrip_url("me@h/M", environ=env)
    assert (c.user, c.password) == ("me", "envp")
    assert parse_ntrip_url("me@h/M", password="opt", environ=env).password == "opt"
    assert parse_ntrip_url("me:@h/M", environ=env).password == ""      # explicitly empty


def test_the_password_is_never_in_a_label_or_repr():
    c = parse_ntrip_url("ntrip://me:hunter2@h:2101/M", environ={})
    assert "hunter2" not in c.label and "hunter2" not in repr(c) and "hunter2" not in str(c)
    assert "me" not in c.label


@pytest.mark.parametrize("spec, message", [
    ("", "needs a caster"),
    ("h", "needs a mountpoint"),
    ("ftp://h/M", "unknown scheme"),
    ("ntrip:///M", "needs a caster host"),
    ("ntrip://h:99999/M", "Port out of range"),
    ("ntrip://me:hunter2@h:99999/M", "^((?!hunter2).)*$"),     # the error never shows the password
])
def test_bad_urls(spec, message):
    with pytest.raises(ValueError, match=message):
        parse_ntrip_url(spec, environ={})


def test_listing_does_not_need_a_mountpoint():
    assert parse_ntrip_url("h:2101", environ={}, need_mountpoint=False).mountpoint == ""


def test_request_forms():
    v2 = build_request(NtripConfig("h", 2101, "M", "u", "p")).decode()
    assert v2.startswith("GET /M HTTP/1.1\r\n") and "Ntrip-Version: Ntrip/2.0\r\n" in v2
    assert "Host: h:2101\r\n" in v2 and "Authorization: Basic dTpw\r\n" in v2
    assert v2.endswith("\r\n\r\n")
    v1 = build_request(NtripConfig("h", 2101, "M", version=1)).decode()
    assert v1.startswith("GET /M HTTP/1.0\r\n") and "Ntrip-Version" not in v1
    assert "Authorization" not in v1                                   # no user, no header
    assert build_request(NtripConfig("h", 2101, "M"), "").decode().startswith("GET / HTTP/1.1")


# ----------------------------------------------------------------- wire format
def test_chunked_decoder_any_split_gives_the_same_bytes():
    payload = bytes(range(256)) * 3
    wire = b"".join(b"%x;ext=1\r\n" % len(payload[i:i + 100]) + payload[i:i + 100] + b"\r\n"
                    for i in range(0, len(payload), 100)) + b"0\r\n\r\n"
    for step in (1, 3, 7, len(wire)):
        d = ChunkedDecoder()
        out = b"".join(d.feed(wire[i:i + step]) for i in range(0, len(wire), step))
        assert out == payload and d.finished, step


def test_chunked_decoder_rejects_garbage():
    with pytest.raises(NtripError, match="malformed"):
        ChunkedDecoder().feed(b"zz\r\nabc")
    with pytest.raises(NtripError, match="malformed"):
        ChunkedDecoder().feed(b"x" * 100)


def test_parse_reply_forms():
    r = parse_reply(b"ICY 200 OK\r\n\r\n\xd3\x00")
    assert (r.status_line, r.rest) == ("ICY 200 OK", b"\xd3\x00")
    r = parse_reply(b"ICY 200 OK\r\n\xd3\x00\x13")                     # bare: data at once
    assert r.rest == b"\xd3\x00\x13"
    assert parse_reply(b"ICY 200 OK\r\n") is None                     # nothing after it yet
    r = parse_reply(b"ICY 200 OK\r\nServer: x\r\nContent-Type: gnss/data\r\n\r\n\xd3")
    assert r.headers["server"] == "x" and r.rest == b"\xd3"
    r = parse_reply(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc")
    assert r.headers["transfer-encoding"] == "chunked" and r.rest == b"5\r\nabc"
    assert parse_reply(b"HTTP/1.1 200 OK\r\nContent-Ty") is None
    assert parse_reply(b"SOURCETABLE 200 OK\r\nServer: x\r\n\r\nSTR;a").status_line.startswith("SOURCE")


def test_reply_classification():
    cfg = NtripConfig("h", 2101, "M", "u", "p")
    assert check_reply(Reply("ICY 200 OK", {}, b""), cfg) is False
    assert check_reply(Reply("HTTP/1.1 200 OK", {}, b""), cfg) is False
    assert check_reply(Reply("HTTP/1.1 200 OK", {"transfer-encoding": "Chunked"}, b""), cfg) is True
    with pytest.raises(NtripMountpointError, match="no mountpoint 'M'"):
        check_reply(Reply("SOURCETABLE 200 OK", {}, b""), cfg)
    with pytest.raises(NtripMountpointError):
        check_reply(Reply("HTTP/1.1 200 OK", {"content-type": "gnss/sourcetable"}, b""), cfg)
    with pytest.raises(NtripAuthError, match="401"):
        check_reply(Reply("HTTP/1.1 401 Unauthorized", {}, b""), cfg)
    with pytest.raises(NtripAuthError, match="403"):
        check_reply(Reply("HTTP/1.1 403 Forbidden", {}, b""), cfg)
    with pytest.raises(NtripAuthError):
        check_reply(Reply("ERROR - Bad Password", {}, b""), cfg)
    with pytest.raises(NtripMountpointError, match="404"):
        check_reply(Reply("HTTP/1.1 404 Not Found", {}, b""), cfg)
    with pytest.raises(NtripFatalError, match="version 1"):
        check_reply(Reply("HTTP/1.1 405 Method Not Allowed", {}, b""), cfg)
    with pytest.raises(NtripFatalError, match="redirected to http://x"):
        check_reply(Reply("HTTP/1.1 302 Found", {"location": "http://x"}, b""), cfg)
    with pytest.raises(NtripError) as exc:
        check_reply(Reply("HTTP/1.1 503 Service Unavailable", {}, b""), cfg)
    assert exc.value.fatal is False                                    # worth retrying
    assert check_reply(Reply("SOURCETABLE 200 OK", {}, b""), cfg, listing=True) is False


def test_sourcetable_parse():
    streams = parse_sourcetable(SOURCETABLE + "STR;short\r\nCAS;x;y\r\n")
    assert [s.mountpoint for s in streams] == ["RTCM", "VRS"]
    rtcm_s, vrs = streams
    assert (rtcm_s.nmea, vrs.nmea) == (False, True)
    assert rtcm_s.format == "RTCM 3.3" and rtcm_s.bitrate == 9600
    assert (rtcm_s.latitude, rtcm_s.longitude) == (37.77, -122.42)


# ----------------------------------------------------------------- client against the fake caster
@pytest.mark.parametrize("style", ["v1", "v1-bare", "v2", "v2-chunked"])
def test_stream_arrives_byte_exact_in_every_reply_style(style):
    async def scenario():
        caster = FakeCaster(style=style, rate=0.02)
        port = await caster.start()
        client = NtripClient(cfg_for(port, version=1 if style.startswith("v1") else 2))
        try:
            got = await client.connect()
            while len(got) < 600:
                data = await client.read(2.0)
                assert data is not None
                got += data
            framer = rtcm.RtcmFramer()
            frames = framer.feed(got)
            assert framer.bad_crc == 0 and framer.skipped == 0         # nothing injected
            assert got == caster.sent[:len(got)]
            assert [f.type for f in frames[:4]] == [1005, 1077, 1087, 1230]
        finally:
            await client.close()
            await caster.stop()
    run(scenario())


def test_request_sent_to_the_caster_carries_version_credentials_and_agent():
    async def scenario():
        caster = FakeCaster(user="me", password="secret", rate=0.02)
        port = await caster.start()
        client = NtripClient(cfg_for(port, user="me", password="secret"))
        try:
            await client.connect()
            req = caster.requests[0]
            assert req["line"] == "GET /RTCM HTTP/1.1" and req["mount"] == "RTCM"
            assert req["headers"]["ntrip-version"] == "Ntrip/2.0"
            assert req["headers"]["user-agent"].startswith("NTRIP gpsview/")
            assert req["headers"]["authorization"].startswith("Basic ")
        finally:
            await client.close()
            await caster.stop()
    run(scenario())


def test_wrong_credentials_and_wrong_mountpoint_are_distinguished():
    async def scenario():
        caster = FakeCaster(user="me", password="secret")
        port = await caster.start()
        try:
            c = NtripClient(cfg_for(port, user="me", password="WRONG-hunter2"))
            with pytest.raises(NtripAuthError) as exc:
                await c.connect()
            assert "hunter2" not in str(exc.value) and "WRONG" not in str(exc.value)
            await c.close()
            c = NtripClient(cfg_for(port, "NOPE", user="me", password="secret"))
            with pytest.raises(NtripMountpointError, match="no mountpoint 'NOPE'"):
                await c.connect()
            await c.close()
            c = NtripClient(cfg_for(port, "NOPE", user="me", password="secret", version=1))
            with pytest.raises(NtripMountpointError):                    # SOURCETABLE 200 OK
                await c.connect()
            await c.close()
        finally:
            await caster.stop()
    run(scenario())


def test_fetch_sourcetable_lists_the_mountpoints():
    async def scenario():
        for version in (1, 2):
            caster = FakeCaster()
            port = await caster.start()
            try:
                streams = await fetch_sourcetable(cfg_for(port, "", version=version))
                assert [(s.mountpoint, s.nmea) for s in streams] == [("RTCM", False), ("VRS", True)]
                assert caster.requests[0]["line"].startswith("GET / HTTP/")
            finally:
                await caster.stop()
    run(scenario())


def test_connection_refused_is_an_oserror_the_session_can_retry():
    async def scenario():
        c = NtripClient(cfg_for(1))                                   # nothing listens on port 1
        with pytest.raises(OSError):
            await c.connect()
    run(scenario())


# ----------------------------------------------------------------- the session
def test_session_forwards_whole_frames_byte_exact():
    async def scenario():
        caster = FakeCaster(style="v2-chunked", rate=0.02)
        port = await caster.start()
        sink = CaptureSink()
        notes = []
        session = NtripSession(cfg_for(port), sink, notify=lambda t, s: notes.append(t))
        task = asyncio.create_task(session.run())
        try:
            assert await until(lambda: session.frames_in >= 12)
            assert session.state == "streaming"
            assert all(f[0] == rtcm.PREAMBLE and rtcm.check(f)[0] == "ok"
                       for batch in sink.batches for f in batch)         # whole, CRC-good frames
            assert sink.data == caster.sent[:len(sink.data)]              # nothing added or lost
            assert session.types.keys() >= {1005, 1077, 1087, 1230}
            assert session.bytes_in >= len(sink.data)
            assert session.age() is not None and session.age() < 2
            assert "NTRIP: streaming ntrip://127.0.0.1" in notes[0]
            text = session.status_text()
            assert "streaming" in text and "capture" in text
            assert not any(str(t) in text for t in session.types)       # types live in the Counts tab
            assert session.types_out.keys() == session.types.keys()     # delivered = received
            assert session.seen_in.keys() == session.seen_out.keys() == session.types.keys()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await caster.stop()
    run(scenario())


def test_junk_between_frames_is_not_forwarded():
    class Junk(NtripClient):
        async def connect(self, **kw):
            return b"\x00\x01garbage" + frames_for(1)[0] + b"\xd3\xff\xff" + frames_for(2)[1]

        async def read(self, timeout):
            await asyncio.sleep(0.05)
            return None

        async def close(self):
            pass

    async def scenario():
        sink = CaptureSink()
        session = NtripSession(cfg_for(1), sink, client_factory=Junk)
        task = asyncio.create_task(session.run())
        assert await until(lambda: session.frames_in >= 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert sink.data == frames_for(1)[0] + frames_for(2)[1]
    run(scenario())


def test_auth_failure_is_reported_once_and_not_retried():
    async def scenario():
        caster = FakeCaster(user="me", password="secret")
        port = await caster.start()
        notes = []
        session = NtripSession(cfg_for(port, user="me", password="hunter2"), CaptureSink(),
                               notify=lambda t, s: notes.append(t), backoff=(0.05, 0.1))
        try:
            await asyncio.wait_for(session.run(), 5)                    # returns: fatal
            assert session.state == "failed" and caster.connections == 1
            assert any("rejected the username or password" in n and "Not retrying" in n
                       for n in notes)
            assert "hunter2" not in " ".join(notes) + session.status_text() + session.error
        finally:
            await caster.stop()
    run(scenario())


def test_wrong_mountpoint_is_fatal_too():
    async def scenario():
        caster = FakeCaster()
        port = await caster.start()
        session = NtripSession(cfg_for(port, "NOPE"), CaptureSink())
        try:
            await asyncio.wait_for(session.run(), 5)
            assert session.state == "failed" and "no mountpoint 'NOPE'" in session.error
        finally:
            await caster.stop()
    run(scenario())


def test_session_reconnects_after_the_stream_drops_and_after_refusal():
    async def scenario():
        caster = FakeCaster(rate=0.02, drop_after=3)                      # 3 epochs, then close
        port = await caster.start()
        sink = CaptureSink()
        notes = []
        session = NtripSession(cfg_for(port), sink, notify=lambda t, s: notes.append(t),
                               backoff=(0.05, 0.1))
        task = asyncio.create_task(session.run())
        try:
            assert await until(lambda: session.connects >= 3)
            assert caster.connections >= 3
            assert any("reconnecting in" in n for n in notes)
            assert session.frames_in >= 8
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await caster.stop()
        # caster gone: connection refused is retried with backoff, not fatal
        session = NtripSession(cfg_for(port), CaptureSink(), backoff=(0.02, 0.05))
        task = asyncio.create_task(session.run())
        await asyncio.sleep(0.3)
        assert session.state in ("waiting", "connecting")
        assert session.error and session.connects == 0                   # refused, still trying
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_idle_caster_is_dropped_and_reconnected():
    async def scenario():
        caster = FakeCaster(rate=60)                                       # one epoch, then silence
        port = await caster.start()
        notes = []
        session = NtripSession(cfg_for(port), CaptureSink(), idle_timeout=0.3,
                               notify=lambda t, s: notes.append(t), backoff=(0.05, 0.1))
        task = asyncio.create_task(session.run())
        try:
            assert await until(lambda: session.connects >= 2, timeout=8)
            assert any("no data from the caster for" in n for n in notes)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await caster.stop()
    run(scenario())


# ----------------------------------------------------------------- GGA (VRS)
GGA = b"$GPGGA,140509.25,3746.4940000,N,12225.1640000,W,1,12,0.9,12.346,M,,M,,*00\r\n"


def test_a_vrs_mountpoint_sends_nothing_until_it_gets_a_gga():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        port = await caster.start()
        position = {"gga": None}
        session = NtripSession(cfg_for(port, "VRS", gga_interval=0.2), CaptureSink(),
                               gga=lambda: position["gga"])
        task = asyncio.create_task(session.run())
        try:
            await asyncio.sleep(0.6)
            assert session.state == "streaming" and session.frames_in == 0   # waiting for GGA
            assert caster.gga == []                                          # no position yet
            position["gga"] = GGA
            assert await until(lambda: session.frames_in >= 4)
            assert caster.gga and caster.gga[0] == GGA                       # byte-exact line
            n = len(caster.gga)
            assert await until(lambda: len(caster.gga) > n)                  # and repeated
            assert session.gga_sent >= 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await caster.stop()
    run(scenario())


def test_no_gga_is_sent_unless_asked_for():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        port = await caster.start()
        session = NtripSession(cfg_for(port, "RTCM", gga_interval=0.0), CaptureSink(),
                               gga=lambda: GGA)                             # a position exists
        task = asyncio.create_task(session.run())
        try:
            assert await until(lambda: session.frames_in >= 8)
            assert caster.gga == [] and session.gga_sent == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await caster.stop()
    run(scenario())


# ----------------------------------------------------------------- sink trouble
def test_a_sink_going_down_is_reported_and_the_session_keeps_streaming():
    class Flaky(CaptureSink):
        async def send(self, frames):
            if self.state == "up" and self.batches:
                self.state, self.detail = "down", "the cable fell out"
                self.dropped += sum(map(len, frames))
                return
            await super().send(frames)

    async def scenario():
        caster = FakeCaster(rate=0.02)
        port = await caster.start()
        notes = []
        sink = Flaky()
        session = NtripSession(cfg_for(port), sink, notify=lambda t, s: notes.append(t))
        task = asyncio.create_task(session.run())
        try:
            assert await until(lambda: sink.dropped > 0 and session.frames_in >= 12)
            assert session.state == "streaming"
            assert any("cannot send corrections to capture: the cable fell out" in n for n in notes)
            assert "dropped" in session.status_text()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await caster.stop()
    run(scenario())


def test_status_text_drops_the_least_useful_parts_first():
    sink = CaptureSink()
    sink.bytes_out = 123456
    session = NtripSession(NtripConfig("h", 2101, "MOUNT"), sink)
    session.state, session.frames_in, session.bytes_in = "streaming", 9, 123456
    session.types.update([1005, 1077, 1087, 1230, 1074, 1084, 1094])
    import time
    session.last_frame = time.monotonic()
    full = session.status_text("receiver: RTCM ok 9 bad 0")
    assert full.startswith("ntrip MOUNT streaming") and "out 123.5 kB" in full and "1005" not in full
    for width in (100, 80, 60, 45):
        text = session.status_text("receiver: RTCM ok 9 bad 0", width)
        assert len(text) <= width, (width, text)
        assert text.startswith("ntrip MOUNT streaming")
    assert "receiver: RTCM ok 9 bad 0" in session.status_text("receiver: RTCM ok 9 bad 0", 70)
    assert "age 0 s" in session.status_text("receiver: RTCM ok 9 bad 0", 75)
    assert "out 123.5 kB" not in session.status_text("receiver: RTCM ok 9 bad 0", 70)   # sent goes first
    tiny = session.status_text("receiver: RTCM ok 9 bad 0", 20)
    assert len(tiny) <= 20
    session.state, session.error = "failed", "x" * 200
    assert len(session.status_text("", 60)) <= 60
