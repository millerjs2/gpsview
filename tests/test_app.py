"""End-to-end: the Textual app talking to tools/fake_gpsd.py over a real TCP socket."""
import asyncio
from unittest import mock

from textual.css.query import NoMatches
from textual.widgets import DataTable, Static, TabbedContent

from gpsview import app as app_mod
from gpsview.app import ConnectionScreen, GpsView, HelpScreen
from gpsview.client import parse_source
from gpsview.fmt import DDMM
from gpsview.views import UNITS
from tools.fake_gpsd import FakeGpsd


async def wait_for(cond, tries=200, step=0.05):
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(step)
    return False


def text_of(app, selector):
    """Plain text of a Static (Textual 8 calls the attribute 'content', older ones 'renderable')."""
    w = app.screen_stack[0].query_one(selector, Static)
    return str(getattr(w, "content", None) or getattr(w, "renderable", ""))


async def click_dialog(app, pilot, selector, tries=100):
    """Click a button in the modal dialog once it has been mounted."""
    for _ in range(tries):
        await pilot.pause(0.05)
        try:
            app.screen.query_one(selector)
        except NoMatches:
            continue
        await pilot.click(selector)
        return True
    return False


def run(coro):
    return asyncio.run(coro)


def test_shows_fix_and_satellites():
    async def scenario():
        gps = FakeGpsd(rate=0.2)
        port = await gps.start()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{port}"))
            async with app.run_test(size=(160, 50)) as pilot:
                assert await wait_for(lambda: app.gps.fix.mode == 3)
                await pilot.pause(1.2)              # let refresh_views run
                assert "3D FIX" in text_of(app, "#status")
                assert "Latitude" in text_of(app, "#datatext")
                table = app.screen_stack[0].query_one("#sats", DataTable)
                assert table.row_count == 11
                assert "Used  8" in table.border_title
                assert app.gps.version.proto_major == 16
                assert "3D FIX" in text_of(app, "#status")
        finally:
            await gps.stop()
    run(scenario())


def test_keys_change_the_view():
    async def scenario():
        gps = FakeGpsd(rate=0.2)
        port = await gps.start()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{port}"))
            async with app.run_test(size=(160, 50)) as pilot:
                assert await wait_for(lambda: app.gps.fix.mode == 3)
                await pilot.press("d")
                assert app.opts.deg == DDMM
                await pilot.press("m")
                assert app.opts.units == UNITS["m"]
                await pilot.press("n")
                assert app.opts.units == UNITS["n"]
                await pilot.press("i")
                assert app.opts.units == UNITS["i"]
                await pilot.press("t")
                assert app.opts.magnetic is True
                await pilot.press("s")
                assert app.raw_on is False
                await pilot.press("2")
                assert app.screen_stack[0].query_one(TabbedContent).active == "tab_imu"
                await pilot.press("3")
                assert app.screen_stack[0].query_one(TabbedContent).active == "tab_rtk"
                await pilot.press("1")
                assert app.screen_stack[0].query_one(TabbedContent).active == "tab_gnss"
        finally:
            await gps.stop()
    run(scenario())


def test_raw_pane_receives_json_until_silenced():
    async def scenario():
        gps = FakeGpsd(rate=0.2)
        port = await gps.start()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{port}"))
            async with app.run_test(size=(160, 50)) as pilot:
                assert await wait_for(lambda: app.gps.fix.mode == 3)
                assert app.rx_bytes > 0 and app.gps.sats
                assert len(app._log().lines) > 0          # raw JSON is shown
                await pilot.press("s")                     # raw off
                assert app.raw_on is False
                await pilot.pause(0.1)
                n = len(app._log().lines)
                await pilot.pause(0.8)                     # several gpsd reports arrive
                assert app.rx_bytes > 0
                assert len(app._log().lines) == n          # nothing new written
                await pilot.press("c")                     # clear
                await pilot.pause(0.1)
                assert len(app._log().lines) == 0
        finally:
            await gps.stop()
    run(scenario())


def test_help_popup_opens_and_any_key_closes():
    async def scenario():
        gps = FakeGpsd(rate=0.5)
        port = await gps.start()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{port}"))
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.press("h")
                await pilot.pause(0.1)
                assert isinstance(app.screen, HelpScreen)
                await pilot.press("x")
                await pilot.pause(0.1)
                assert not isinstance(app.screen, HelpScreen)
        finally:
            await gps.stop()
    run(scenario())


def test_no_fix_is_shown_as_no_fix():
    async def scenario():
        gps = FakeGpsd(has_fix=False, rate=0.2)
        port = await gps.start()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{port}"))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.online)
                # the status bar refreshes once a second
                assert await wait_for(lambda: "NO FIX" in text_of(app, "#status"), tries=60)
                assert "Latitude" in text_of(app, "#datatext")
                assert "Latitude         n/a" in text_of(app, "#datatext")
        finally:
            await gps.stop()
    run(scenario())


def test_refused_connection_offers_quit():
    async def scenario():
        gps = FakeGpsd()
        port = await gps.start()
        await gps.stop()                      # nothing listens on this port now
        app = GpsView(parse_source(f"127.0.0.1:{port}"))
        async with app.run_test(size=(160, 50)) as pilot:
            assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
            assert await click_dialog(app, pilot, "#quit")
            assert await wait_for(lambda: not app.is_running)
    run(scenario())


def test_lost_connection_and_retry():
    async def scenario():
        gps = FakeGpsd(rate=0.2)
        port = await gps.start()
        try:
            app = GpsView(parse_source(f"127.0.0.1:{port}"))
            async with app.run_test(size=(160, 50)) as pilot:
                assert await wait_for(lambda: app.gps.fix.mode == 3)
                gps.drop_connections()
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                assert not app.online
                assert await click_dialog(app, pilot, "#retry")
                assert await wait_for(lambda: app.online and app.gps.fix.mode == 3)
        finally:
            await gps.stop()
    run(scenario())


def test_stall_dialog_keep_waiting_then_data_resumes():
    async def scenario():
        gps = FakeGpsd(rate=0.1)
        port = await gps.start()
        try:
            with mock.patch.object(app_mod, "STALL_SECONDS", 1.0), \
                    mock.patch.object(app_mod, "POLL_SECONDS", 0.1):
                app = GpsView(parse_source(f"127.0.0.1:{port}"))
                async with app.run_test(size=(160, 50)) as pilot:
                    assert await wait_for(lambda: app.gps.fix.mode == 3)
                    gps.stalled = True
                    assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                    assert await click_dialog(app, pilot, "#wait")
                    assert await wait_for(lambda: not isinstance(app.screen, ConnectionScreen))
                    gps.stalled = False
                    seen = app.rx_bytes
                    assert await wait_for(lambda: app.rx_bytes > seen)
        finally:
            await gps.stop()
    run(scenario())
