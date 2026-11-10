"""End to end: the app reading a fake UBX receiver, pulling corrections from a fake NTRIP caster,
and writing them back to the receiver on the data link ("same") or on another interface."""
import asyncio
import os
import struct
import threading

import pytest

from gpsview import rtcm
from gpsview.__main__ import main
from gpsview.app import ConnectionScreen, GpsView
from gpsview.corrections import SinkSpec, parse_sink
from gpsview.model import MODE_3D
from gpsview.nmea import NmeaDecoder
from gpsview.ntrip import NtripConfig
from gpsview.transports import parse_endpoint
from gpsview.ubx import make_frame, ubx_feed
from tests.test_app import click_dialog, run, text_of, wait_for
from tests.test_feeds import free_udp_port
from tools.fake_feeds import FakeFeed, make_payload, ubx_epoch
from tools.fake_ntrip import FakeCaster


def parsed(data: bytes) -> rtcm.RtcmFramer:
    framer = rtcm.RtcmFramer()
    framer.frames = framer.feed(bytes(data))
    return framer


def messages_text(app) -> str:
    return "\n".join(line.text for line in app._log().lines)


def make_app(feed_endpoint, caster_port, mount="RTCM", out="same", **kw):
    ep = parse_endpoint(feed_endpoint, "ubx")
    cfg = NtripConfig("127.0.0.1", caster_port, mount, **kw)
    return GpsView(ubx_feed(ep), ntrip=(cfg, parse_sink(out, "ubx", ep)))


def test_corrections_go_back_on_the_data_link_byte_exact():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: len(rx.received) > 1500)
                got = parsed(rx.received)
                assert got.bad_crc == 0 and got.skipped == 0           # whole frames only
                assert bytes(rx.received) == caster.sent[:len(rx.received)]   # exact, in order
                assert app.gps.fix.mode == MODE_3D                     # the data still flows
                assert await wait_for(lambda: "streaming" in text_of(app, "#status"), tries=60)
                status = text_of(app, "#status")
                assert "ntrip RTCM streaming" in status and "1077" not in status   # types are in Counts
                assert "same link" in status or "127.0.0.1" in status
                assert "NTRIP: streaming" in messages_text(app)
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_corrections_can_go_to_a_different_interface_than_the_data_comes_from():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        side = bytearray()

        async def side_port(reader, writer):                           # the receiver's 2nd port
            while data := await reader.read(65536):
                side.extend(data)
        server = await asyncio.start_server(side_port, "127.0.0.1", 0)
        sport = server.sockets[0].getsockname()[1]
        try:
            app = make_app(f"127.0.0.1:{rport}", cport, out=f"tcp://127.0.0.1:{sport}")
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: len(side) > 1500)
                assert bytes(side) == caster.sent[:len(side)]
                assert parsed(side).skipped == 0
                assert bytes(rx.received) == b""                       # NOT on the data link
                assert app.gps.fix.mode == MODE_3D
                assert await wait_for(lambda: f"127.0.0.1:{sport}" in text_of(app, "#status"),
                                      tries=60)
        finally:
            await rx.stop()
            server.close()
            await caster.stop()
    run(scenario())


def test_corrections_to_a_serial_port_other_than_the_data_port():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        data_tty = rx.pty()
        ctl_master, ctl_slave = os.openpty()                           # the receiver's other tty
        os.set_blocking(ctl_master, False)
        got = bytearray()

        async def pump():
            while True:
                try:
                    got.extend(os.read(ctl_master, 65536))
                except (BlockingIOError, OSError):
                    pass
                await asyncio.sleep(0.02)
        pump_task = asyncio.create_task(pump())
        try:
            app = make_app(data_tty, cport, out=os.ttyname(ctl_slave))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: len(got) > 1500, tries=300)
                assert bytes(got) == caster.sent[:len(got)]
                assert rx.received == b""
        finally:
            pump_task.cancel()
            await rx.stop()
            os.close(ctl_master)
            os.close(ctl_slave)
            await caster.stop()
    run(scenario())


def test_same_link_over_a_serial_port():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        tty = rx.pty()
        try:
            app = make_app(tty, cport)                                 # out = same
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: len(rx.received) > 1500, tries=300)
                assert bytes(rx.received) == caster.sent[:len(rx.received)]
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_same_link_over_udp_replies_to_the_receiver():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        port = free_udp_port()
        rx = FakeFeed(make_payload("ubx", True), rate=0.1)
        rx.udp(port)
        try:
            app = make_app(f"udp://127.0.0.1:{port}", cport)
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: len(rx.received) > 1500, tries=300)
                assert parsed(rx.received).skipped == 0                # whole frames, whatever is lost
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_same_link_follows_the_new_connection_after_the_data_link_is_retried():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(160, 50)) as pilot:
                assert await wait_for(lambda: len(rx.received) > 500)
                rx.drop_connections()
                assert await wait_for(lambda: isinstance(app.screen, ConnectionScreen))
                assert app.transport is None                           # no link: nowhere to send
                before = len(rx.received)
                await asyncio.sleep(0.4)
                assert len(rx.received) == before                      # dropped, not queued
                assert app.sink.dropped > 0 or app.sink.state == "down"
                assert await click_dialog(app, pilot, "#retry")
                assert await wait_for(lambda: len(rx.received) > before + 500)
                assert parsed(rx.received).skipped == 0                # still whole frames only
                assert app.transport is not None
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_vrs_mountpoint_gets_the_position_as_a_valid_gga():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport, mount="VRS", gga_interval=0.3)
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: len(rx.received) > 800, tries=300)   # VRS replied
                gga = caster.gga[0]
                assert gga.startswith(b"$GPGGA,") and gga.endswith(b"\r\n")
                dec = NmeaDecoder("x")
                obj = [e.obj for e in dec.feed(gga) if e.kind == "obj"][0]
                assert dec.errors == 0                                  # the checksum is right
                assert abs(obj["lat"] - 37.7749) < 0.002 and abs(obj["lon"] + 122.4194) < 0.002
                assert obj["mode"] == 3
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_the_password_stays_out_of_every_place_the_app_shows_or_writes(tmp_path):
    async def scenario():
        caster = FakeCaster(user="me", password="s3cr3t-pw", rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport, user="me", password="s3cr3t-pw")
            app.log_dir = str(tmp_path)
            async with app.run_test(size=(160, 50)) as pilot:
                assert await wait_for(lambda: len(rx.received) > 500)
                app.action_toggle_log()
                await pilot.pause(0.5)
                seen = " ".join([messages_text(app), text_of(app, "#status"), app.sub_title,
                                 app.title, repr(app.ntrip.cfg), app.ntrip.cfg.label,
                                 app.ntrip.status_text()])
                assert "s3cr3t-pw" not in seen and "me:" not in seen
            for f in tmp_path.iterdir():
                assert b"s3cr3t-pw" not in f.read_bytes()
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_bad_credentials_are_reported_once_and_the_receiver_keeps_working():
    async def scenario():
        caster = FakeCaster(user="me", password="right")
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport, user="me", password="wrong")
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: "Not retrying" in messages_text(app))
                assert caster.connections == 1
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D)
                assert await wait_for(lambda: "failed" in text_of(app, "#status"), tries=60)
                assert bytes(rx.received) == b""
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_a_correction_port_that_is_missing_is_reported_and_data_still_flows():
    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(make_payload("ubx", True), rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport, out="/dev/no-such-tty")
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: "cannot send corrections" in messages_text(app))
                assert "Dropping them until it works" in messages_text(app)
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D)
                assert await wait_for(lambda: "down" in text_of(app, "#status"), tries=60)
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_the_receivers_own_report_on_the_corrections_is_shown():
    def payload(now):
        report = make_frame(0x02, 0x32, struct.pack("<BBHHH", 2, 0x04, 0, 5, 1077))   # used
        return ubx_epoch(now) + report

    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(payload, rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: "receiver: RTCM ok" in text_of(app, "#status"),
                                      tries=100)
                assert any("RXM-RTCM" in l.text and "1077 crc ok, used" in l.text
                           for l in app._log().lines)
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())


def test_without_ntrip_nothing_is_written_to_the_receiver():
    async def scenario():
        rx = FakeFeed(make_payload("ubx", True), rate=0.1)
        rport = await rx.tcp()
        try:
            app = GpsView(ubx_feed(parse_endpoint(f"127.0.0.1:{rport}", "ubx")))
            async with app.run_test(size=(160, 50)):
                assert await wait_for(lambda: app.gps.fix.mode == MODE_3D)
                await asyncio.sleep(0.5)
                assert bytes(rx.received) == b"" and app.ntrip is None
                assert app.screen_stack[0].query_one("#status").styles.height.value == 3
        finally:
            await rx.stop()
    run(scenario())


# ----------------------------------------------------------------- command line
@pytest.mark.parametrize("argv, message", [
    (["-ubx", "udp://:1", "--rtcm-out", "same"], "only makes sense with --ntrip"),
    (["-ubx", "udp://:1", "--ntrip-gga", "10"], "only makes sense with --ntrip"),
    (["-ubx", "udp://:1", "--ntrip", "caster.example/"], "needs a mountpoint"),
    (["-ubx", "udp://:1", "--ntrip", "ftp://c/M"], "unknown scheme"),
    (["-ubx", "udp://:1", "--ntrip", "c/M", "--rtcm-out", "udp://:5"], "needs a host to send to"),
    (["-ubx", "udp://:1", "--ntrip", "c/M", "--ntrip-gga", "-1"], "--ntrip-gga"),
    (["pi.local", "--ntrip", "c/M"], "gpsd owns the receiver"),
    (["-ubx", "file:///dev/null", "--ntrip", "c/M"], "not a regular file"),
])
def test_ntrip_option_refusals(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_a_file_source_needs_an_explicit_destination(tmp_path, capsys):
    f = tmp_path / "x.ubx"
    f.write_bytes(b"")
    with pytest.raises(SystemExit):
        main(["-ubx", str(f), "--ntrip", "c/M"])
    assert "a file cannot take corrections" in capsys.readouterr().err


def test_gpsd_source_with_an_explicit_destination_is_accepted(monkeypatch):
    import gpsview.__main__ as cli
    started = {}
    monkeypatch.setattr(cli.GpsView, "run", lambda self: started.setdefault("ntrip", self.ntrip))
    assert main(["pi.local", "--ntrip", "me:pw@c.example/M", "--rtcm-out", "/dev/ttyUSB1:9600",
                 "--ntrip-gga", "10"]) == 0
    session = started["ntrip"]
    assert session.cfg.gga_interval == 10 and session.cfg.user == "me"
    assert session.sink.spec == SinkSpec("serial", path="/dev/ttyUSB1", baud=9600)


def test_same_tty_for_data_and_corrections_is_one_link(monkeypatch):
    import gpsview.__main__ as cli
    started = {}
    monkeypatch.setattr(cli.GpsView, "run", lambda self: started.setdefault("sink", self.sink))
    main(["-ubx", "/dev/ttyACM0", "--ntrip", "c/M", "--rtcm-out", "/dev/ttyACM0"])
    assert started["sink"].label == "same link"


class CasterThread:
    """A fake caster on its own event loop, for the CLI, which runs its own loop."""

    def __init__(self, **kw):
        self.caster = FakeCaster(**kw)
        self.loop = asyncio.new_event_loop()
        self.port = 0
        self._t = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        self.port = asyncio.run_coroutine_threadsafe(self.caster.start(), self.loop).result(5)
        return self

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.caster.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._t.join(5)


def test_ntrip_list_prints_the_mountpoints(capsys):
    with CasterThread() as c:
        assert main(["--ntrip-list", f"127.0.0.1:{c.port}"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split()[:3] == ["MOUNTPOINT", "FORMAT", "SYSTEMS"]
    assert lines[1].startswith("RTCM") and lines[2].startswith("VRS") and "gga" in lines[2]


def test_ntrip_list_strips_terminal_escapes_from_the_casters_text(capsys):
    evil = ("STR;EVIL\x1b]0;pwned\x07;x\x1b[2J;RTCM 3;;2;GPS;N;USA;1;2;0;1;g;none;N;N;1;\r\n"
            "ENDSOURCETABLE\r\n")
    with CasterThread(sourcetable=evil) as c:
        assert main(["--ntrip-list", f"127.0.0.1:{c.port}"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "EVIL" in out


def test_ntrip_list_reports_an_unreachable_caster(capsys):
    assert main(["--ntrip-list", "127.0.0.1:1"]) == 1
    assert "gpsview:" in capsys.readouterr().err


# ----------------------------------------------------------------- the documented usage
def free_tcp_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_readme_try_it_commands_work_as_written(monkeypatch):
    """Start the fake receiver and the fake caster from their command lines, then run gpsview's
    own command line with the credential form the README shows: a user in the URL and the
    password in NTRIP_PASSWORD."""
    import subprocess
    import sys
    import time
    import gpsview.__main__ as cli
    root = os.path.dirname(os.path.dirname(__file__))
    rport, cport = free_tcp_port(), free_tcp_port()
    env = {**os.environ, "PYTHONPATH": root}
    procs = [
        subprocess.Popen([sys.executable, "-I", os.path.join(root, "tools", "fake_ntrip.py"),
                          "--port", str(cport), "--user", "me", "--password", "secret",
                          "--rate", "0.05"], env=env, cwd=root, stdout=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "-I", os.path.join(root, "tools", "fake_feeds.py"),
                          "ubx", "--tcp", str(rport), "--rate", "0.2"], env=env, cwd=root,
                         stdout=subprocess.DEVNULL),
    ]
    result = {}

    def drive(app):
        async def scenario():
            async with app.run_test(size=(80, 24)):
                assert await wait_for(lambda: app.ntrip.frames_in > 8 and app.gps.fix.mode == MODE_3D,
                                      tries=200)
                assert await wait_for(lambda: "ntrip RTCM streaming" in text_of(app, "#status"),
                                      tries=60)             # the bar redraws once a second
                result["state"] = app.ntrip.state
                result["status"] = text_of(app, "#status")
                result["fix"] = app.gps.fix.mode
        asyncio.run(scenario())
    monkeypatch.setattr(cli.GpsView, "run", drive)
    monkeypatch.setenv("NTRIP_PASSWORD", "secret")
    monkeypatch.delenv("NTRIP_USER", raising=False)
    try:
        for _ in range(100):                                       # wait for both to listen
            try:
                for port in (rport, cport):
                    import socket
                    socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        assert cli.main(["-ubx", f"tcp://127.0.0.1:{rport}", "--ntrip",
                         f"me@127.0.0.1:{cport}/RTCM", "--rtcm-out", "same"]) == 0
    finally:
        for p in procs:
            p.terminate()
            p.wait(5)
    assert result["state"] == "streaming" and result["fix"] == MODE_3D
    assert "ntrip RTCM streaming" in result["status"]


def test_the_ntrip_status_line_fits_80_columns_and_keeps_the_receivers_report():
    def payload(now):
        return ubx_epoch(now) + make_frame(0x02, 0x32, struct.pack("<BBHHH", 2, 0x04, 0, 5, 1077))

    async def scenario():
        caster = FakeCaster(rate=0.02)
        cport = await caster.start()
        rx = FakeFeed(payload, rate=0.2)
        rport = await rx.tcp()
        try:
            app = make_app(f"127.0.0.1:{rport}", cport)
            async with app.run_test(size=(80, 24)):
                assert await wait_for(lambda: "RTCM ok" in text_of(app, "#status"), tries=100)
                lines = text_of(app, "#status").split("\n")
                assert len(lines) == 4 and all(len(l) <= 78 for l in lines[3:])
                assert "receiver: RTCM ok" in lines[3] and "same link up" in lines[3]
                bar = app.screen_stack[0].query_one("#status")
                assert bar.region.height == 4
                assert app.screen_stack[0].query_one("#messages").region.height >= 5
        finally:
            await rx.stop()
            await caster.stop()
    run(scenario())
