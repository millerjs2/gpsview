"""End-to-end: the Textual app reading NMEA and SBP from the fake receiver over TCP, UDP and
a pty (standing in for a serial tty).  No gpsd is involved."""
import socket

from textual.widgets import DataTable

from gpsview.app import GpsView
from gpsview.feeds import Feed
from gpsview.nmea import nmea_feed
from gpsview.swift import sbp_feed
from gpsview.transports import parse_endpoint
from tests.test_app import click_dialog, run, text_of, wait_for
from tools.fake_feeds import FakeFeed, make_payload, nmea_epoch


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_nmea_over_tcp_shows_fix_and_satellites():
    async def scenario():
        fake = FakeFeed(make_payload("nmea", True), rate=0.2)
        port = await fake.tcp()
        try:
            app = GpsView(nmea_feed(parse_endpoint(f"127.0.0.1:{port}", "nmea")))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.gps.fix.mode == 3)
                # the status bar refreshes once a second: wait for it to show the fix
                assert await wait_for(lambda: "3D FIX" in text_of(app, "#status"), tries=60)
                status = text_of(app, "#status")
                assert "3D FIX" in status and "bad checksums 0" in status
                assert "tcp://127.0.0.1:" in status
                assert await wait_for(lambda: app.gps.sats_used == 8)
                table = app.screen_stack[0].query_one("#sats", DataTable)
                assert table.row_count == 10          # 8 GPS + 2 GLONASS in view
                assert "Used  8" in table.border_title
                assert app.gps.leap_seconds is None
                assert "(n/a)" in text_of(app, "#datatext")   # leap seconds not reported
        finally:
            await fake.stop()
    run(scenario())


def test_nmea_checksum_errors_are_counted_in_the_status_bar():
    async def scenario():
        bad = b"$GPGGA,123519,,,,,0,00,99.9,,M,,M,,*00\r\n"
        fake = FakeFeed(lambda now: nmea_epoch(now) + bad, rate=0.2)
        port = await fake.tcp()
        try:
            app = GpsView(nmea_feed(parse_endpoint(f"127.0.0.1:{port}", "nmea")))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.gps.fix.mode == 3)
                assert await wait_for(lambda: "bad checksums" in text_of(app, "#status")
                                      and "bad checksums 0" not in text_of(app, "#status"),
                                      tries=60)
        finally:
            await fake.stop()
    run(scenario())


def test_sbp_over_udp_shows_fix_satellites_and_base():
    async def scenario():
        port = free_udp_port()
        fake = FakeFeed(make_payload("sbp", True), rate=0.2)
        fake.udp(port)
        try:
            app = GpsView(sbp_feed(parse_endpoint(f"udp://127.0.0.1:{port}", "sbp")))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.gps.fix.mode == 3, tries=300)
                # SBAS position (flags 6) is DGPS in gpsd's status terms
                assert await wait_for(lambda: "3D DGPS FIX" in text_of(app, "#status"), tries=60)
                assert "udp://127.0.0.1:" in text_of(app, "#status")
                assert "bad CRC 0" in text_of(app, "#status")
                assert app.gps.fix.base.status == 3          # fixed RTK base line
                assert any("SBP PosLLH" in line.text for line in app._log().lines)
        finally:
            await fake.stop()
    run(scenario())


def test_sbp_no_fix_is_shown_as_no_fix():
    async def scenario():
        fake = FakeFeed(make_payload("sbp", False), rate=0.2)
        port = await fake.tcp()
        try:
            app = GpsView(sbp_feed(parse_endpoint(f"127.0.0.1:{port}", "sbp")))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.online)
                assert await wait_for(lambda: "NO FIX" in text_of(app, "#status"), tries=60)
                assert "Latitude         n/a" in text_of(app, "#datatext")
        finally:
            await fake.stop()
    run(scenario())


def test_nmea_over_a_pty_serial_port():
    async def scenario():
        fake = FakeFeed(make_payload("nmea", True), rate=0.2)
        tty = fake.pty()
        try:
            app = GpsView(nmea_feed(parse_endpoint(tty, "nmea")))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.gps.fix.mode == 3, tries=300)
                assert tty in text_of(app, "#status")
                assert "device" in text_of(app, "#status")
        finally:
            await fake.stop()
    run(scenario())


def test_serial_port_that_does_not_exist_offers_retry_or_quit():
    async def scenario():
        feed = nmea_feed(parse_endpoint("/dev/this-tty-does-not-exist", "nmea"))
        app = GpsView(feed)
        async with app.run_test(size=(160, 50)) as pilot:
            from gpsview.app import ConnectionScreen
            assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
            assert await click_dialog(app, pilot, "#quit")
            assert await wait_for(lambda: not app.is_running)
    run(scenario())


def test_feeds_are_plain_values():
    feed = sbp_feed(parse_endpoint("172.16.0.120:55555", "sbp"))
    assert isinstance(feed, Feed)
    assert (feed.title, feed.label, feed.header) == ("SBP", "tcp://172.16.0.120:55555", "SBP")
    assert nmea_feed(parse_endpoint("/dev/ttyUSB0", "nmea")).header == "NMEA 0183"
