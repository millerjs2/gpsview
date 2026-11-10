"""Autobaud: scoring valid frames, the scan, and its use from the app and the CLI."""
import asyncio
import binascii
import os
import random

import pytest

import gpsview.app as appmod
from gpsview.app import ConnectionScreen, GpsView
from gpsview.autobaud import Scorer, candidate_bauds, detect_baud
from gpsview.model import MODE_3D
from gpsview.nmea import checksum
from gpsview.sources import baud_text, make_feed
from gpsview.transports import Transport
from gpsview.ubx import make_frame as ubx_frame
from tests.test_app import click_dialog, run, wait_for
from tools.fake_feeds import FakeFeed, make_payload


def nmea(body="GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"):
    return f"${body}*{checksum(body):02X}\r\n".encode()


def sbp(msg_type=0x20A, payload=b"\x01\x02\x03\x04"):
    body = msg_type.to_bytes(2, "little") + (0x42).to_bytes(2, "little") + bytes([len(payload)]) + payload
    return b"\x55" + body + binascii.crc_hqx(body, 0).to_bytes(2, "little")


def ubx():
    return ubx_frame(0x01, 0x04, bytes(18))


FRAMES = {"nmea": nmea, "ubx": ubx, "sbp": sbp}


def noise(n=4000, seed=1):
    return random.Random(seed).randbytes(n)


# ----------------------------------------------------------------- scoring
@pytest.mark.parametrize("protocol", ["nmea", "ubx", "sbp"])
def test_valid_frames_are_counted_however_the_bytes_are_split(protocol):
    stream = b"".join(FRAMES[protocol]() for _ in range(5))
    for chunk in (len(stream), 1, 7, 13):
        s = Scorer(protocol)
        for i in range(0, len(stream), chunk):
            s.feed(stream[i:i + chunk])
        assert s.frames == 5, (protocol, chunk)


@pytest.mark.parametrize("protocol", ["nmea", "ubx", "sbp"])
def test_noise_and_corrupt_frames_score_nothing(protocol):
    s = Scorer(protocol)
    s.feed(noise(20000))
    assert s.frames == 0
    bad = bytearray(FRAMES[protocol]())
    bad[-3 if protocol == "nmea" else -1] ^= 0x55           # break the checksum
    s = Scorer(protocol)
    s.feed(bytes(bad) * 3)
    assert s.frames == 0


def test_frames_between_noise_are_found():
    s = Scorer("ubx")
    s.feed(noise(100, 2) + ubx() + noise(50, 3) + ubx())
    assert s.frames == 2


def test_nmea_without_a_checksum_does_not_count():
    s = Scorer("nmea")
    s.feed(b"$GPGGA,1,2,3\r\n" * 5)
    assert s.frames == 0


def test_scorer_rejects_gpsd():
    with pytest.raises(ValueError):
        Scorer("gpsd")


def test_candidate_order_starts_with_preferred_then_default():
    assert candidate_bauds("ubx")[:2] == [38400, 115200]
    assert candidate_bauds("nmea", 4800)[:3] == [4800, 9600, 115200]
    assert len(candidate_bauds("sbp")) == len(set(candidate_bauds("sbp")))


# ----------------------------------------------------------------- the scan
class FakeSerial(Transport):
    """A receiver that sends frames only when opened at its real rate; noise at any other."""
    opened: list = []
    closed = 0

    def __init__(self, protocol, true_rate, mode="match", path="/dev/fake", baud=0):
        self.protocol, self.true_rate, self.mode, self.baud = protocol, true_rate, mode, baud
        self.label = path
        self._n = 0

    async def open(self):
        if self.mode == "missing":
            raise FileNotFoundError("no such device")
        FakeSerial.opened.append(self.baud)

    async def read(self, timeout):
        await asyncio.sleep(min(timeout, 0.01))
        self._n += 1
        if self.mode == "silent":
            return None
        if self.baud == self.true_rate:
            return FRAMES[self.protocol]()
        return noise(200, self._n)

    async def close(self):
        FakeSerial.closed += 1


def factory(protocol, true_rate, mode="match"):
    FakeSerial.opened, FakeSerial.closed = [], 0
    return lambda path, baud: FakeSerial(protocol, true_rate, mode, path, baud)


def test_scan_finds_the_rate_and_stops_there():
    async def scenario():
        seen = []
        r = await detect_baud("/dev/fake", "ubx", dwell=0.5, progress=seen.append,
                              make_transport=factory("ubx", 9600))
        assert r.baud == 9600 and r.attempts[-1].frames >= 2
        assert seen == FakeSerial.opened == [38400, 115200, 9600]      # nothing after the hit
        assert FakeSerial.closed == 3                                  # every port was closed
    run(scenario())


def test_scan_of_a_silent_port_says_so():
    async def scenario():
        r = await detect_baud("/dev/fake", "nmea", dwell=0.05, make_transport=factory("nmea", 9600, "silent"))
        assert r.baud is None and not r.saw_bytes and "No data" in r.summary()
        assert len(r.attempts) == len(candidate_bauds("nmea"))
    run(scenario())


def test_scan_where_nothing_decodes_says_so():
    async def scenario():
        r = await detect_baud("/dev/fake", "nmea", dwell=0.05, make_transport=factory("nmea", 1234))
        assert r.baud is None and r.saw_bytes and "no valid frames" in r.summary()
    run(scenario())


def test_scan_reports_a_port_that_cannot_be_opened_and_stops():
    async def scenario():
        r = await detect_baud("/dev/fake", "ubx", make_transport=factory("ubx", 9600, "missing"))
        assert r.baud is None and "Cannot open /dev/fake" in r.summary() and not r.attempts
        assert FakeSerial.closed == 1
    run(scenario())


def test_cancelling_a_scan_closes_the_port():
    async def scenario():
        task = asyncio.create_task(detect_baud(
            "/dev/fake", "ubx", dwell=30, make_transport=factory("ubx", 1, "silent")))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert FakeSerial.closed == 1
    run(scenario())


# ----------------------------------------------------------------- source forms
def test_auto_on_a_tty_marks_the_endpoint():
    f = make_feed("ubx", "/dev/ttyUSB0", baud="auto")
    assert f.endpoint.autobaud and baud_text(f) == "auto"
    assert make_feed("ubx", "/dev/ttyUSB0", baud="AUTO").endpoint.autobaud
    assert make_feed("ubx", "/dev/ttyUSB0").endpoint.autobaud            # the default now
    assert not make_feed("ubx", "/dev/ttyUSB0:9600").endpoint.autobaud
    assert not make_feed("ubx", "/dev/ttyUSB0", baud=9600).endpoint.autobaud
    assert baud_text(make_feed("ubx", "/dev/ttyUSB0:9600")) == "9600"
    assert baud_text(make_feed("ubx", "10.0.0.1:5000")) == ""


@pytest.mark.parametrize("protocol, spec, message", [
    ("ubx", "10.0.0.1:5000", "only applies to a tty"),
    ("gpsd", "", "only applies to a tty"),
    ("ubx", "/dev/ttyUSB0:9600", "nothing to detect"),
])
def test_auto_is_refused_where_it_makes_no_sense(protocol, spec, message):
    with pytest.raises(ValueError, match=message):
        make_feed(protocol, spec, baud="auto")


# ----------------------------------------------------------------- in the app
def patch_detect(monkeypatch, protocol, true_rate, calls):
    real = appmod.detect_baud

    async def fake(path, proto, **kw):
        calls.append(path)
        return await real(path, proto, dwell=0.05, make_transport=factory(protocol, true_rate))
    monkeypatch.setattr(appmod, "detect_baud", fake)


def test_app_detects_then_connects_at_that_rate(monkeypatch):
    async def scenario():
        calls = []
        patch_detect(monkeypatch, "ubx", 115200, calls)
        fake = FakeFeed(make_payload("ubx", True), rate=0.2)
        tty = fake.pty()
        try:
            app = GpsView(make_feed("ubx", tty, baud="auto"))
            async with app.run_test(size=(100, 40)):
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D, tries=300)
                assert calls == [tty]
                assert app.feed.endpoint.baud == 115200 and not app.feed.endpoint.autobaud
        finally:
            await fake.stop()
    run(scenario())


def test_app_offers_detect_again_or_quit_when_nothing_is_found(monkeypatch):
    async def scenario():
        calls = []
        patch_detect(monkeypatch, "ubx", 1234, calls)
        fake = FakeFeed(make_payload("ubx", True), rate=0.2)
        tty = fake.pty()
        try:
            app = GpsView(make_feed("ubx", tty, baud="auto"))
            async with app.run_test(size=(100, 40)) as pilot:
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                assert "No baud rate" in str(app.screen._title)
                assert "no valid frames" in app.screen._detail
                assert await click_dialog(app, pilot, "#retry")
                assert await wait_for(lambda: len(calls) == 2)              # scanned again
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                assert await click_dialog(app, pilot, "#quit")
                assert await wait_for(lambda: not app.is_running)
        finally:
            await fake.stop()
    run(scenario())


def test_stall_dialog_offers_auto_baud_for_a_tty_and_it_works(monkeypatch):
    async def scenario():
        monkeypatch.setattr(appmod, "STALL_SECONDS", 0.4)
        monkeypatch.setattr(appmod, "POLL_SECONDS", 0.1)
        calls = []
        patch_detect(monkeypatch, "ubx", 9600, calls)
        master, slave = os.openpty()
        tty = os.ttyname(slave)                                  # silent at first
        try:
            app = GpsView(make_feed("ubx", tty, baud="38400"))     # explicit: no scan at start
            async with app.run_test(size=(80, 24)) as pilot:
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                await pilot.pause(0.3)
                for b in ("#retry", "#wait", "#detect", "#configure", "#quit"):
                    assert app.screen.query_one(b).region.right <= 80, b     # all fit at 80 columns
                assert await click_dialog(app, pilot, "#detect")
                assert await wait_for(lambda: calls == [tty])
                assert await wait_for(lambda: app.feed.endpoint.baud == 9600
                                      and not app.feed.endpoint.autobaud)
        finally:
            os.close(master)
            os.close(slave)
    run(scenario())


def test_stall_dialog_has_no_auto_baud_for_a_network_source(monkeypatch):
    async def scenario():
        monkeypatch.setattr(appmod, "STALL_SECONDS", 0.4)
        monkeypatch.setattr(appmod, "POLL_SECONDS", 0.1)

        async def silent(reader, writer):
            await asyncio.sleep(30)
        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            app = GpsView(make_feed("ubx", f"127.0.0.1:{port}"))
            async with app.run_test(size=(100, 40)):
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                assert not app.screen.query("#detect")
        finally:
            server.close()
    run(scenario())


def test_cli_accepts_baud_auto():
    from gpsview.__main__ import build_parser
    assert build_parser().parse_args(["-ubx", "/dev/ttyUSB0", "-b", "auto"]).baud == "auto"


def test_scan_stops_listening_as_soon_as_enough_frames_arrive():
    async def scenario():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        r = await detect_baud("/dev/fake", "ubx", candidates=[9600], dwell=5.0,
                              make_transport=factory("ubx", 9600))
        assert r.baud == 9600 and loop.time() - t0 < 1.0
    run(scenario())
