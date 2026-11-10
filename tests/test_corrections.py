"""Correction sinks: the spec grammar, "same link" (which must follow the app's current transport),
and separate interfaces (tty, TCP, UDP).  Bytes must arrive exactly as RTCM frames."""
import asyncio
import os
import socket

import pytest

from gpsview import rtcm
from gpsview.corrections import (UDP_PAYLOAD, SameLinkSink, SinkSpec, TransportSink, _batches,
                                 make_sink, parse_sink)
from gpsview.transports import Endpoint, Transport, UdpTransport
from tools.fake_ntrip import frames_for


def run(coro):
    return asyncio.run(coro)


def frames(n=3):
    return [f for seq in range(n) for f in frames_for(seq)]


# ----------------------------------------------------------------- spec grammar
def test_sink_spec_forms():
    assert parse_sink("same", "ubx") == SinkSpec("same")
    assert parse_sink("/dev/ttyUSB1", "ubx") == SinkSpec("serial", path="/dev/ttyUSB1", baud=38400)
    assert parse_sink("/dev/ttyUSB1:9600", "sbp").baud == 9600
    assert parse_sink("/dev/ttyUSB1", "sbp").baud == 115200
    assert parse_sink("10.0.0.5:5000", "ubx") == SinkSpec("tcp", host="10.0.0.5", port=5000)
    assert parse_sink("tcp://rx.local:5000", "ubx").kind == "tcp"
    assert parse_sink("udp://10.0.0.5:5001", "ubx") == SinkSpec("udp", host="10.0.0.5", port=5001)
    assert parse_sink("udp://[::1]:5001", "ubx").host == "::1"
    assert parse_sink("udp://10.0.0.5:5001", "ubx").label == "udp://10.0.0.5:5001"


@pytest.mark.parametrize("spec, message", [
    ("", "needs a destination"),
    ("file:///tmp/x", "cannot be a file"),
    ("http://x:1", "unknown scheme"),
    ("udp://:5001", "needs a host to send to"),      # a UDP *output* has a destination, not a bind
    ("host", "give a port"),
    ("host:0", "give a port"),
])
def test_bad_sink_specs(spec, message):
    with pytest.raises(ValueError, match=message):
        parse_sink(spec, "ubx")


def test_naming_the_source_tty_means_same():
    src = Endpoint("serial", path="/dev/ttyACM0", baud=38400)
    assert parse_sink("/dev/ttyACM0", "ubx", src) == SinkSpec("same")      # one open per tty
    assert parse_sink("/dev/ttyACM0:9600", "ubx", src) == SinkSpec("same")
    assert parse_sink("/dev/ttyACM1", "ubx", src).kind == "serial"
    assert parse_sink("/dev/ttyACM0", "ubx", Endpoint("tcp", host="h", port=1)).kind == "serial"


def test_same_tty_is_recognised_through_a_symlink(tmp_path):
    real = tmp_path / "ttyX"
    real.write_text("")
    link = tmp_path / "by-id-receiver"
    link.symlink_to(real)
    src = Endpoint("serial", path=str(real), baud=9600)
    assert parse_sink(str(link), "ubx", src) == SinkSpec("same")


def test_batches_never_split_a_frame():
    fs = frames(10)
    batches = _batches(fs, UDP_PAYLOAD)
    assert b"".join(batches) == b"".join(fs)
    assert all(len(b) <= UDP_PAYLOAD for b in batches) and len(batches) > 1
    for b in batches:                                  # each datagram parses on its own
        got = rtcm.RtcmFramer().feed(b)
        assert sum(len(f.data) for f in got) == len(b)
    assert _batches([b"x" * 3000], 1400) == [b"x" * 3000]    # an oversize frame goes alone


# ----------------------------------------------------------------- same link
class FakeTransport(Transport):
    def __init__(self, label="fake"):
        self.label = label
        self.written = bytearray()
        self.fail = False

    async def write(self, data):
        if self.fail:
            raise ConnectionError("the link is down")
        self.written += data


def test_same_link_follows_the_current_transport_across_reconnects():
    async def scenario():
        current = {"t": None}
        sink = SameLinkSink(lambda: current["t"])
        fs = frames(2)
        await sink.send(fs)                                    # nothing connected: dropped
        assert sink.state == "down" and sink.dropped == sum(map(len, fs))
        assert "not connected" in sink.detail
        first = current["t"] = FakeTransport("tcp://rx:1")
        await sink.send(fs)
        assert bytes(first.written) == b"".join(fs) and sink.state == "up"
        assert sink.dest_label == "tcp://rx:1 (same link)"
        second = current["t"] = FakeTransport("tcp://rx:1")   # the session made a new one
        await sink.send(fs)
        assert bytes(second.written) == b"".join(fs)           # the old one was not used again
        assert bytes(first.written) == b"".join(fs)
        assert sink.bytes_out == 2 * sum(map(len, fs))
    run(scenario())


def test_same_link_write_failure_drops_and_recovers():
    async def scenario():
        t = FakeTransport()
        sink = SameLinkSink(lambda: t)
        t.fail = True
        await sink.send(frames(1))
        assert sink.state == "down" and "link is down" in sink.detail and sink.dropped > 0
        t.fail = False
        await sink.send(frames(1))
        assert sink.state == "up"
    run(scenario())


def test_same_link_over_a_slow_write_does_not_block_the_caller():
    class Hung(FakeTransport):
        async def write(self, data):
            await asyncio.sleep(60)

    async def scenario():
        import gpsview.corrections as c
        old, c.SEND_TIMEOUT = c.SEND_TIMEOUT, 0.1
        try:
            sink = SameLinkSink(lambda: Hung())
            await asyncio.wait_for(sink.send(frames(1)), 2)
            assert sink.state == "down" and sink.dropped > 0
        finally:
            c.SEND_TIMEOUT = old
    run(scenario())


def test_same_link_udp_replies_to_the_sender_in_whole_frame_datagrams():
    async def scenario():
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2.0)
        t = UdpTransport("127.0.0.1", 0)
        await t.open()
        port = t._transport.get_extra_info("sockname")[1]
        sink = SameLinkSink(lambda: t)
        await sink.send(frames(1))
        assert sink.state == "down" and "no datagram" in sink.detail      # no peer yet
        rx.sendto(b"$GPGGA*00\r\n", ("127.0.0.1", port))
        await t.read(2.0)
        fs = frames(12)
        await sink.send(fs)
        got = b""
        while len(got) < sum(map(len, fs)):
            dgram = rx.recvfrom(4096)[0]
            assert len(dgram) <= UDP_PAYLOAD
            assert sum(len(f.data) for f in rtcm.RtcmFramer().feed(dgram)) == len(dgram)
            got += dgram
        assert got == b"".join(fs)
        rx.close()
        await t.close()
    run(scenario())


# ----------------------------------------------------------------- separate interfaces
def test_tcp_sink_delivers_exact_bytes_and_reconnects_after_the_peer_drops():
    async def scenario():
        received, conns = bytearray(), []

        async def handle(reader, writer):
            conns.append(writer)
            while data := await reader.read(65536):
                received.extend(data)
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sink = TransportSink(SinkSpec("tcp", host="127.0.0.1", port=port))
        fs = frames(4)
        try:
            await sink.send(fs)
            await asyncio.sleep(0.1)
            assert bytes(received) == b"".join(fs) and sink.state == "up"
            conns[0].close()                                       # the receiver hangs up
            for _ in range(100):
                if sink.state == "down":
                    break
                await asyncio.sleep(0.02)
            assert sink.state == "down"
            before = sink.dropped
            await sink.send(fs)                                    # dropped, not queued
            assert sink.dropped > before
            sink._next_try = 0                                     # skip the backoff wait
            await sink.send(fs)
            await asyncio.sleep(0.1)
            assert bytes(received).endswith(b"".join(fs)) and sink.state == "up"
        finally:
            await sink.close()
            server.close()
    run(scenario())


def test_tcp_sink_that_cannot_connect_drops_and_backs_off():
    async def scenario():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        now = [100.0]
        sink = TransportSink(SinkSpec("tcp", host="127.0.0.1", port=port), loop_time=lambda: now[0])
        fs = frames(1)
        await sink.send(fs)
        assert sink.state == "down" and sink.dropped == sum(map(len, fs)) and sink.detail
        tries = sink._backoff
        await sink.send(fs)                                       # inside the backoff: no new attempt
        assert sink._backoff == tries
        now[0] += 5
        await sink.send(fs)
        assert sink._backoff > tries and sink.dropped == 3 * sum(map(len, fs))
    run(scenario())


def test_udp_sink_sends_whole_frame_datagrams_to_a_fixed_address():
    async def scenario():
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2.0)
        sink = TransportSink(SinkSpec("udp", host="127.0.0.1", port=rx.getsockname()[1]))
        fs = frames(10)
        await sink.send(fs)
        got = b""
        while len(got) < sum(map(len, fs)):
            dgram = rx.recvfrom(4096)[0]
            assert sum(len(f.data) for f in rtcm.RtcmFramer().feed(dgram)) == len(dgram)
            got += dgram
        assert got == b"".join(fs)
        await sink.close()
        rx.close()
    run(scenario())


def test_serial_sink_writes_exact_bytes_to_a_tty():
    async def scenario():
        master, slave = os.openpty()
        os.set_blocking(master, False)
        sink = TransportSink(SinkSpec("serial", path=os.ttyname(slave), baud=115200))
        fs = frames(2)
        try:
            await sink.send(fs)
            await asyncio.sleep(0.1)
            assert os.read(master, 65536) == b"".join(fs)
            assert sink.state == "up"
        finally:
            await sink.close()
            os.close(master)
            os.close(slave)
    run(scenario())


def test_serial_sink_for_a_missing_tty_is_down_not_an_exception():
    async def scenario():
        sink = TransportSink(SinkSpec("serial", path="/dev/no-such-tty", baud=9600))
        await sink.send(frames(1))
        assert sink.state == "down" and sink.dropped > 0
    run(scenario())


def test_make_sink_picks_the_class():
    assert isinstance(make_sink(SinkSpec("same"), lambda: None), SameLinkSink)
    assert isinstance(make_sink(SinkSpec("udp", host="h", port=1), lambda: None), TransportSink)
