"""Choosing the input: the baud rate (--baud and the dialog), and "Configure another input"
from the connection dialog and the o key."""
import socket

import pytest
from textual.widgets import Input, Select, Static

from gpsview.__main__ import build_parser
from gpsview.app import ConnectionScreen, GpsView, SourceScreen
from gpsview.client import parse_source
from gpsview.model import MODE_3D
from gpsview.sources import find_ttys, make_feed, parse_baud
from tests.test_app import click_dialog, run, text_of, wait_for
from tools.fake_feeds import FakeFeed, make_payload
from tools.fake_gpsd import FakeGpsd


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ----------------------------------------------------------------- baud rate
def test_baud_forms_for_a_tty():
    assert make_feed("ubx", "/dev/ttyUSB0").endpoint.baud == 38400          # protocol default
    assert make_feed("ubx", "/dev/ttyUSB0:9600").endpoint.baud == 9600      # suffix
    assert make_feed("ubx", "/dev/ttyUSB0", baud="115200").endpoint.baud == 115200
    assert make_feed("nmea", "/dev/ttyUSB0", baud=4800).endpoint.baud == 4800
    assert make_feed("ubx", "/dev/ttyUSB0:9600", baud="9600").endpoint.baud == 9600
    assert make_feed("sbp", "/dev/ttyUSB0", baud="").endpoint.baud == 115200


@pytest.mark.parametrize("protocol, spec, baud, message", [
    ("ubx", "/dev/ttyUSB0:9600", "115200", "give only one"),
    ("ubx", "10.0.0.1:5000", "9600", "only applies to a tty"),
    ("ubx", "udp://:5000", "9600", "only applies to a tty"),
    ("ubx", "/dev/ttyUSB0", "fast", "give a number"),
    ("ubx", "/dev/ttyUSB0", "0", "give a number"),
    ("ubx", "/dev/ttyUSB0", "-9600", "give a number"),
    ("gpsd", "", "9600", "only applies to a tty"),
    ("ubx", "", "", "give a source"),
    ("nope", "x:1", "", "unknown protocol"),
])
def test_bad_baud_and_source(protocol, spec, baud, message):
    with pytest.raises(ValueError, match=message):
        make_feed(protocol, spec, baud=baud)


def test_parse_baud():
    assert parse_baud(None) is None and parse_baud("  ") is None and parse_baud(" 9600 ") == 9600


def test_cli_baud_option_reaches_the_feed():
    args = build_parser().parse_args(["-ubx", "/dev/ttyUSB0", "--baud", "115200"])
    assert args.baud == "115200"
    assert make_feed("ubx", args.ubx, baud=args.baud).endpoint.baud == 115200


def test_gpsd_feed_can_be_rebuilt_from_its_spec():
    from gpsview.sources import feed_spec
    assert feed_spec(make_feed("gpsd", "pi.local:2947:/dev/ttyACM0")) == "pi.local:2947:/dev/ttyACM0"
    assert feed_spec(make_feed("ubx", "/dev/ttyUSB0:9600")) == "/dev/ttyUSB0"
    assert feed_spec(make_feed("ubx", "10.0.0.1:5000")) == "tcp://10.0.0.1:5000"


def test_find_ttys_returns_a_list():
    assert isinstance(find_ttys(), list)


# ----------------------------------------------------------------- the dialogs
def fill(app, spec=None, baud=None, protocol=None):
    if protocol is not None:
        app.screen.query_one("#src_proto", Select).value = protocol
    if spec is not None:
        app.screen.query_one("#src_spec", Input).value = spec
    if baud is not None:
        app.screen.query_one("#src_baud", Input).value = baud


def error_text(app) -> str:
    w = app.screen.query_one("#src_error", Static)
    return str(getattr(w, "content", None) or getattr(w, "renderable", ""))


def test_no_gpsd_offers_retry_quit_and_configure_another_input():
    async def scenario():
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{closed_port()}"))      # gpsd is not running
            async with app.run_test(size=(100, 40)) as pilot:
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                await pilot.pause(0.3)
                for button in ("#retry", "#configure", "#quit"):
                    assert app.screen.query_one(button).region.right <= 100
                assert str(app.screen.query_one("#configure").label) == "Configure another input"
                assert await click_dialog(app, pilot, "#configure")
                assert await wait_for(lambda: isinstance(app.screen, SourceScreen))
                assert app.screen.query_one("#src_proto", Select).value == "gpsd"
                fill(app, protocol="ubx", spec=f"127.0.0.1:{rport}")
                assert await click_dialog(app, pilot, "#connect")
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D, tries=300)
                assert app.feed.protocol == "ubx" and not isinstance(app.screen, SourceScreen)
                assert "UBX" in text_of(app, "#status")
        finally:
            await rx.stop()
    run(scenario())


def test_cancelling_returns_to_the_connection_dialog_and_bad_input_is_explained():
    async def scenario():
        app = GpsView(parse_source(f"127.0.0.1:{closed_port()}"))
        async with app.run_test(size=(100, 40)) as pilot:
            assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
            assert await click_dialog(app, pilot, "#configure")
            assert await wait_for(lambda: isinstance(app.screen, SourceScreen))
            fill(app, protocol="ubx", spec="", baud="")
            assert await click_dialog(app, pilot, "#connect")
            await pilot.pause(0.3)
            assert await wait_for(lambda: "give a source" in error_text(app))
            fill(app, spec="10.0.0.1:5000", baud="9600")
            assert await click_dialog(app, pilot, "#connect")
            await pilot.pause(0.3)
            assert await wait_for(lambda: "only applies to a tty" in error_text(app))
            fill(app, spec="/dev/ttyUSB0", baud="loud")
            assert await click_dialog(app, pilot, "#connect")
            await pilot.pause(0.3)
            assert await wait_for(lambda: "give a number" in error_text(app))
            assert isinstance(app.screen, SourceScreen) and app.feed.protocol == "gpsd"
            assert await click_dialog(app, pilot, "#cancel")
            assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
            assert await click_dialog(app, pilot, "#quit")
            assert await wait_for(lambda: not app.is_running)
    run(scenario())


def test_a_tty_with_a_baud_rate_from_the_dialog():
    async def scenario():
        fake = FakeFeed(make_payload("ubx", True), rate=0.2)
        tty = fake.pty()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{closed_port()}"))
            async with app.run_test(size=(100, 40)) as pilot:
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                assert await click_dialog(app, pilot, "#configure")
                assert await wait_for(lambda: isinstance(app.screen, SourceScreen))
                fill(app, protocol="ubx", spec=tty, baud="115200")
                assert await click_dialog(app, pilot, "#connect")
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D, tries=300)
                assert app.feed.endpoint.baud == 115200 and app.feed.endpoint.path == tty
        finally:
            await fake.stop()
    run(scenario())


def test_o_key_switches_input_while_running_and_forgets_the_old_receiver():
    async def scenario():
        gps = FakeGpsd(rate=0.2)
        gport = await gps.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{gport}"))
            async with app.run_test(size=(100, 40)) as pilot:
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D)
                await pilot.press("o")
                assert await wait_for(lambda: isinstance(app.screen, SourceScreen))
                assert app.screen.query_one("#src_spec", Input).value == f"127.0.0.1:{gport}"
                fill(app, protocol="ubx")                                  # stale gpsd text goes
                await pilot.pause(0.2)
                assert app.screen.query_one("#src_spec", Input).value != f"127.0.0.1:{gport}"
                fill(app, spec=f"127.0.0.1:{rport}")
                assert await click_dialog(app, pilot, "#connect")
                assert await wait_for(lambda: app.feed.protocol == "ubx")
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D and app.online,
                                      tries=300)
                assert "UBX" in text_of(app, "#status")
                assert app.sub_title == f"tcp://127.0.0.1:{rport}"
                await pilot.press("o")                          # prefilled with what is running
                assert await wait_for(lambda: isinstance(app.screen, SourceScreen))
                assert app.screen.query_one("#src_proto", Select).value == "ubx"
                assert await click_dialog(app, pilot, "#cancel")
                assert await wait_for(lambda: not isinstance(app.screen, SourceScreen))
                assert app.feed.protocol == "ubx"
        finally:
            await rx.stop()
            await gps.stop()
    run(scenario())


def test_o_key_is_ignored_while_a_dialog_is_open():
    async def scenario():
        app = GpsView(parse_source(f"127.0.0.1:{closed_port()}"))
        async with app.run_test(size=(100, 40)) as pilot:
            assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
            await pilot.press("o")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConnectionScreen)
    run(scenario())


def test_input_dialog_fits_an_80x24_terminal():
    async def scenario():
        app = GpsView(parse_source(f"127.0.0.1:{closed_port()}"))
        async with app.run_test(size=(80, 24)) as pilot:
            assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
            await pilot.pause(0.3)
            for button in ("#retry", "#configure", "#quit"):
                assert app.screen.query_one(button).region.right <= 80
            assert await click_dialog(app, pilot, "#configure")
            assert await wait_for(lambda: isinstance(app.screen, SourceScreen))
            await pilot.pause(0.3)
            box = app.screen.query_one("#source")
            assert box.region.height <= 24 and box.region.width <= 80
            assert app.screen.query_one("#connect").region.bottom <= 24
    run(scenario())
