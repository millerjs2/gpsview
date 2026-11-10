"""The Counts tab (lower pane, beside Messages): what the receiver sent, and the RTCM that came
in from the caster and went out to the receiver."""
from textual.widgets import DataTable, TabbedContent

from gpsview.app import GpsView
from gpsview.client import parse_source
from gpsview.nmea import NmeaDecoder
from gpsview.rtcm import describe
from gpsview.ubx import UbxDecoder
from tests.test_app import run, wait_for
from tests.test_ntrip_app import make_app
from tools.fake_feeds import FakeFeed, make_payload, nmea_epoch, ubx_epoch
from tools.fake_gpsd import FakeGpsd
from tools.fake_ntrip import FakeCaster


def rows_of(app, table):
    t = app.screen_stack[0].query_one(table, DataTable)
    return [tuple(str(c) for c in t.get_row_at(i)) for i in range(t.row_count)]


def table_rows(app):
    return rows_of(app, "#counts")                    # Receiver: message, count, last


def rtcm_rows(app):
    return rows_of(app, "#rtcm_counts")               # type, name, in, out, last in


def test_describe_names_common_rtcm_messages():
    assert describe(1005) == "Station ARP"
    assert describe(1077) == "GPS MSM7" and describe(1124) == "BeiDou MSM4"
    assert describe(1087) == "GLONASS MSM7" and describe(1097) == "Galileo MSM7"
    assert describe(1230) == "GLONASS code-phase biases"
    assert describe(1070) == "" and describe(9999) == ""


def test_decoders_tag_messages_for_the_counts():
    nm = [e.msg for e in NmeaDecoder("").feed(nmea_epoch(1.0, True)) if e.kind == "raw"]
    assert "GPGGA" in nm and "" not in nm
    ub = [e.msg for e in UbxDecoder("").feed(ubx_epoch(1.0, True)) if e.kind == "raw"]
    assert "NAV-PVT" in ub and "" not in ub
    bad = NmeaDecoder("").feed(b"$GPGGA,1,2,3*00\r\n")                 # bad checksum: not counted
    assert [e.msg for e in bad if e.kind == "raw"] == [""]


def test_counts_tab_shows_receiver_messages_in_ubx_nmea_sbp_and_gpsd():
    async def scenario():
        gps = FakeGpsd(rate=0.2)
        gport = await gps.start()
        feeds = []
        for protocol in ("ubx", "nmea", "sbp"):
            fake = FakeFeed(make_payload(protocol, True), rate=0.2)
            feeds.append((protocol, fake, await fake.tcp()))
        try:
            for protocol, fake, port in feeds + [("gpsd", None, gport)]:
                if protocol == "gpsd":
                    app = GpsView(parse_source(f"127.0.0.1:{port}"))
                else:
                    from gpsview.sources import make_feed
                    app = GpsView(make_feed(protocol, f"127.0.0.1:{port}"))
                async with app.run_test(size=(100, 40)) as pilot:
                    assert await wait_for(lambda: len(app.msg_counts) >= 2, tries=100), protocol
                    await pilot.press("5")
                    assert await wait_for(lambda: app.screen_stack[0].query_one(
                        "#msgtabs", TabbedContent).active == "tab_counts")
                    assert await wait_for(lambda: table_rows(app))
                    rows = table_rows(app)
                    assert all(int(r[1]) >= 1 for r in rows)
                    expect = {"ubx": "NAV-PVT", "nmea": "GPGGA", "sbp": "PosLLH", "gpsd": "TPV"}
                    assert expect[protocol] in {r[0] for r in rows}, (protocol, rows)
                    assert rtcm_rows(app) == []                    # no NTRIP: nothing there
        finally:
            for _, fake, _ in feeds:
                await fake.stop()
            await gps.stop()
    run(scenario())


def test_counts_include_rtcm_in_and_out_and_keep_counting():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.press("5")
                assert await wait_for(lambda: len(rtcm_rows(app)) == 4, tries=200)
                rows = {r[0]: r for r in rtcm_rows(app)}
                assert set(rows) == {"1005", "1077", "1087", "1230"}
                assert rows["1077"][1] == "GPS MSM7" and rows["1005"][1] == "Station ARP"
                first = sum(int(r[2]) for r in rows.values())
                assert await wait_for(lambda: sum(int(r[2]) for r in rtcm_rows(app))
                                      > first + 8, tries=200)             # live updates
                for _type, _name, got, sent, last in rtcm_rows(app):
                    assert 0 <= int(got) - int(sent) <= 2                  # out follows in
                    assert last.endswith(" s")
                t = app.screen_stack[0].query_one("#rtcm_counts", DataTable)
                assert [str(c.label) for c in t.columns.values()] == [
                    "RTCM", "Name", "In", "Out", "Last in"]
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_both_tables_fit_100_columns_and_show_at_least_the_header_and_rows():
    async def scenario():
        caster = FakeCaster(rate=0.05)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.press("5")
                assert await wait_for(lambda: len(rtcm_rows(app)) == 4 and table_rows(app))
                await pilot.pause(0.5)
                base = app.screen_stack[0]
                left, right = base.query_one("#counts"), base.query_one("#rtcm_counts")
                assert left.region.right <= right.region.x and right.region.right <= 100
                assert right.region.height >= 5                    # header and a few rows
                assert right.virtual_size.width <= right.region.width     # no sideways scroll
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_status_line_no_longer_carries_the_types_and_fits_100_columns():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(100, 40)):
                assert await wait_for(lambda: app.ntrip and app.ntrip.frames_in > 20)
                assert await wait_for(lambda: len(app._ntrip_line()) > 30)
                line = app._ntrip_line()
                assert len(line) <= 98 and "1077" not in line
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_c_clears_the_counts_when_that_tab_is_showing_and_the_messages_otherwise():
    async def scenario():
        caster = FakeCaster(rate=0.5)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(100, 40)) as pilot:
                assert await wait_for(lambda: app.msg_counts and app.ntrip.types_out)
                await pilot.press("c")                       # Messages is showing: log cleared
                assert app.msg_counts and app.ntrip.types_out
                await pilot.press("5")
                await pilot.pause(0.3)
                await pilot.press("c")                       # Counts is showing: counts cleared
                await pilot.pause(0.1)
                assert sum(c for c, _ in app.msg_counts.values()) < 20
                assert sum(app.ntrip.types_out.values()) < 20
                await pilot.press("4")
                assert app.screen_stack[0].query_one("#msgtabs", TabbedContent).active == "tab_messages"
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_switching_input_clears_the_receiver_counts():
    async def scenario():
        rx = FakeFeed(make_payload("ubx", True), rate=0.1)
        rport = await rx.tcp()
        try:
            from gpsview.sources import make_feed
            app = GpsView(make_feed("ubx", f"127.0.0.1:{rport}"))
            async with app.run_test(size=(100, 40)):
                assert await wait_for(lambda: app.msg_counts)
                app._adopt(make_feed("ubx", f"127.0.0.1:{rport}"))
                assert app.msg_counts == {}
        finally:
            await rx.stop()
    run(scenario())
