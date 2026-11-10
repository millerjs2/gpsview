"""The raw data log: file name, exclusive creation, and the app writing the received bytes."""
import os
from datetime import datetime, timedelta, timezone

from gpsview.app import GpsView
from gpsview.logfile import RawLog, log_name
from gpsview.nmea import nmea_feed
from gpsview.swift import sbp_feed
from gpsview.transports import parse_endpoint
from tests.test_app import run, text_of, wait_for
from tools.fake_feeds import FakeFeed, make_payload


def test_name_is_protocol_and_utc_minute():
    when = datetime(2026, 11, 10, 13, 43, 5, tzinfo=timezone.utc)
    assert log_name("sbp", when) == "gpsview_sbp_20261110_1343Z.log"
    assert log_name("nmea", when) == "gpsview_nmea_20261110_1343Z.log"
    assert log_name("gpsd", when) == "gpsview_gpsd_20261110_1343Z.log"


def test_local_time_is_converted_to_utc():
    # 08:43 at UTC-5 is 13:43Z; the name uses UTC whatever the local timezone is
    when = datetime(2026, 11, 10, 8, 43, tzinfo=timezone(timedelta(hours=-5)))
    assert log_name("sbp", when) == "gpsview_sbp_20261110_1343Z.log"


def test_same_minute_gets_a_suffix_and_nothing_is_overwritten(tmp_path):
    when = datetime(2026, 11, 10, 13, 43, tzinfo=timezone.utc)
    first = RawLog(str(tmp_path), "sbp", when)
    first.write(b"first")
    first.close()
    second = RawLog(str(tmp_path), "sbp", when)
    second.write(b"second")
    second.close()
    assert os.path.basename(first.path) == "gpsview_sbp_20261110_1343Z.log"
    assert os.path.basename(second.path) == "gpsview_sbp_20261110_1343Z_2.log"
    assert open(first.path, "rb").read() == b"first"
    assert open(second.path, "rb").read() == b"second"


def test_writes_are_bytes_as_received(tmp_path):
    log = RawLog(str(tmp_path), "sbp")
    log.write(b"\x55\x00\x01")                       # binary is kept as it is
    log.write(b"\xff")
    log.close()
    log.close()                                       # closing twice is harmless
    assert open(log.path, "rb").read() == b"\x55\x00\x01\xff"
    assert log.bytes_written == 4


def test_missing_directory_is_an_oserror(tmp_path):
    try:
        RawLog(str(tmp_path / "no-such-dir"), "sbp")
    except OSError:
        return
    raise AssertionError("expected OSError")


def test_app_logs_everything_it_receives_from_the_start(tmp_path):
    async def scenario():
        fake = FakeFeed(make_payload("nmea", True), rate=0.2)
        port = await fake.tcp()
        try:
            app = GpsView(nmea_feed(parse_endpoint(f"127.0.0.1:{port}", "nmea")),
                          log_dir=str(tmp_path), log_at_start=True)
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.gps.fix.mode == 3)
                assert await wait_for(lambda: "log on" in text_of(app, "#status"), tries=60)
                path = app.raw_log.path
                assert os.path.basename(path).startswith("gpsview_nmea_")
                assert await wait_for(lambda: app.rx_bytes > 2000)
                received = app.rx_bytes
            # the file holds exactly what was received, since logging started at launch
            data = open(path, "rb").read()
            assert len(data) == received
            assert data.startswith(b"$GP")
        finally:
            await fake.stop()
    run(scenario())


def test_w_key_starts_and_stops_logging(tmp_path):
    async def scenario():
        fake = FakeFeed(make_payload("sbp", True), rate=0.2)
        port = await fake.tcp()
        try:
            app = GpsView(sbp_feed(parse_endpoint(f"127.0.0.1:{port}", "sbp")),
                          log_dir=str(tmp_path))
            async with app.run_test(size=(160, 50)) as pilot:
                assert await wait_for(lambda: app.rx_bytes > 0)
                assert app.raw_log is None
                await pilot.press("w")
                await pilot.pause(0.1)
                path = app.raw_log.path
                assert await wait_for(lambda: os.path.getsize(path) > 0 if os.path.exists(path) else False)
                await pilot.press("w")
                await pilot.pause(0.1)
                assert app.raw_log is None
                size = os.path.getsize(path)
                await pilot.pause(0.5)                          # more data arrives, not logged
                assert os.path.getsize(path) == size
            # SBP frames begin with the 0x55 preamble
            assert open(path, "rb").read()[:1] == b"\x55"
            assert os.path.basename(path).startswith("gpsview_sbp_")
        finally:
            await fake.stop()
    run(scenario())


def test_default_log_directory_is_where_gpsview_was_started(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = GpsView(sbp_feed(parse_endpoint("127.0.0.1:9", "sbp")))
    assert app.log_dir == str(tmp_path)
