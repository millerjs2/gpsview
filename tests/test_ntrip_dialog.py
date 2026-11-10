"""The RTK dialog (key r): start, restart, stop, validation, and that the secrets stay in memory."""
import asyncio

from textual.widgets import Button, Footer, Input, Static

from gpsview.app import GpsView, NtripScreen
from gpsview.model import MODE_3D
from gpsview.transports import parse_endpoint
from gpsview.ubx import ubx_feed
from tests.test_app import click_dialog, run, text_of, wait_for
from tests.test_ntrip_app import messages_text, parsed
from tools.fake_feeds import FakeFeed, make_payload
from tools.fake_gpsd import FakeGpsd
from tools.fake_ntrip import FakeCaster


def fill(app, **values):
    for name, value in values.items():
        app.screen.query_one(f"#ntrip_{name}", Input).value = value


async def open_dialog(app, pilot):
    await pilot.press("r")
    assert await wait_for(lambda: isinstance(app.screen, NtripScreen))


def error_text(app) -> str:
    w = app.screen.query_one("#ntrip_error", Static)
    return str(getattr(w, "content", None) or getattr(w, "renderable", ""))


def test_start_from_the_dialog_sends_corrections_then_stop_stops_them():
    async def scenario():
        caster = FakeCaster(user="me", password="pw", rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = GpsView(ubx_feed(parse_endpoint(f"127.0.0.1:{rport}", "ubx")))
            async with app.run_test(size=(100, 40)) as pilot:
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D)
                assert app.ntrip is None
                await open_dialog(app, pilot)
                assert app.screen.query_one("#stop", Button).disabled          # nothing to stop
                assert app.screen.query_one("#ntrip_pass", Input).password is True
                fill(app, url=f"127.0.0.1:{cport}/RTCM", user="me", **{"pass": "pw"})
                assert await click_dialog(app, pilot, "#start")
                assert await wait_for(lambda: app.ntrip is not None and len(rx.received) > 800)
                assert bytes(rx.received) == caster.sent[:len(rx.received)]    # on the data link
                assert parsed(rx.received).skipped == 0
                assert await wait_for(lambda: "ntrip RTCM streaming" in text_of(app, "#status"),
                                      tries=60)
                assert len(text_of(app, "#status").split("\n")) == 4
                # reopen: filled in from the running session, and it can be stopped
                await open_dialog(app, pilot)
                assert app.screen.query_one("#ntrip_url", Input).value.endswith(f":{cport}/RTCM")
                assert app.screen.query_one("#ntrip_user", Input).value == "me"
                assert str(app.screen.query_one("#start", Button).label) == "Restart"
                assert "streaming" in str(app.screen.query_one("#ntrip_state", Static).content)
                assert await click_dialog(app, pilot, "#stop")
                assert await wait_for(lambda: app.ntrip is None)
                assert "NTRIP: stopped" in messages_text(app)
                await asyncio.sleep(0.3)
                n = len(rx.received)
                await asyncio.sleep(0.5)
                assert len(rx.received) == n                                   # nothing more sent
                assert await wait_for(lambda: len(text_of(app, "#status").split("\n")) == 3,
                                      tries=60)
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_bad_input_is_explained_in_the_dialog_and_nothing_starts():
    async def scenario():
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = GpsView(ubx_feed(parse_endpoint(f"127.0.0.1:{rport}", "ubx")))
            async with app.run_test(size=(100, 40)) as pilot:
                await open_dialog(app, pilot)
                fill(app, url="caster.example.com")                           # no mountpoint
                assert await click_dialog(app, pilot, "#start")
                await pilot.pause(0.3)          # the error line changes the layout
                assert await wait_for(lambda: "needs a mountpoint" in error_text(app))
                fill(app, url="c.example/M", gga="soon")
                assert await click_dialog(app, pilot, "#start")
                await pilot.pause(0.3)          # the error line changes the layout
                assert await wait_for(lambda: "number of seconds" in error_text(app))
                fill(app, gga="0", out="udp://:5000")
                assert await click_dialog(app, pilot, "#start")
                await pilot.pause(0.3)          # the error line changes the layout
                assert await wait_for(lambda: "needs a host to send to" in error_text(app))
                assert isinstance(app.screen, NtripScreen) and app.ntrip is None
                assert await click_dialog(app, pilot, "#close")
                assert await wait_for(lambda: not isinstance(app.screen, NtripScreen))
                assert app.ntrip is None
        finally:
            await rx.stop()
    run(scenario())


def test_same_is_refused_for_gpsd_with_the_reason():
    async def scenario():
        gps = FakeGpsd(rate=0.2)
        port = await gps.start()
        try:
            from gpsview.client import parse_source
            app = GpsView(parse_source(f"127.0.0.1:{port}"))
            async with app.run_test(size=(100, 40)) as pilot:
                await open_dialog(app, pilot)
                fill(app, url="c.example/M")
                assert await click_dialog(app, pilot, "#start")
                assert "gpsd owns the receiver" in error_text(app)
        finally:
            await gps.stop()
    run(scenario())


def test_a_separate_destination_can_be_typed_in_the_dialog():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        side = bytearray()

        async def side_port(reader, writer):
            while data := await reader.read(65536):
                side.extend(data)
        server = await asyncio.start_server(side_port, "127.0.0.1", 0)
        sport = server.sockets[0].getsockname()[1]
        try:
            app = GpsView(ubx_feed(parse_endpoint(f"127.0.0.1:{rport}", "ubx")))
            async with app.run_test(size=(100, 40)) as pilot:
                await open_dialog(app, pilot)
                fill(app, url=f"127.0.0.1:{cport}/RTCM", out=f"tcp://127.0.0.1:{sport}")
                assert await click_dialog(app, pilot, "#start")
                assert await wait_for(lambda: len(side) > 800)
                assert bytes(side) == caster.sent[:len(side)] and bytes(rx.received) == b""
        finally:
            await rx.stop()
            server.close()
            await caster.stop()
    run(scenario())


def test_an_empty_password_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("NTRIP_PASSWORD", "from-env")

    async def scenario():
        caster = FakeCaster(user="me", password="from-env", rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = GpsView(ubx_feed(parse_endpoint(f"127.0.0.1:{rport}", "ubx")))
            async with app.run_test(size=(100, 40)) as pilot:
                await open_dialog(app, pilot)
                fill(app, url=f"me@127.0.0.1:{cport}/RTCM")
                assert await click_dialog(app, pilot, "#start")
                assert await wait_for(lambda: app.ntrip is not None and app.ntrip.frames_in > 4)
                assert "from-env" not in messages_text(app) + text_of(app, "#status")
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_the_dialog_fits_a_24_row_terminal_and_the_footer_fits_100_columns():
    async def scenario():
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = GpsView(ubx_feed(parse_endpoint(f"127.0.0.1:{rport}", "ubx")))
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.5)                                # let the footer lay out
                footer = app.screen_stack[0].query_one(Footer)
                assert footer.region.height == 1
                assert footer.virtual_size.width <= 100              # nothing scrolls or wraps
            app = GpsView(ubx_feed(parse_endpoint(f"127.0.0.1:{rport}", "ubx")))
            async with app.run_test(size=(80, 24)) as pilot:
                await open_dialog(app, pilot)
                box = app.screen.query_one("#ntrip")
                assert box.region.height <= 24 and box.region.width <= 80
                assert app.screen.query_one("#start", Button).region.bottom <= 24
        finally:
            await rx.stop()
    run(scenario())
