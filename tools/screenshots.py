"""Regenerate docs/screenshots/*.svg: the app at 100x40 in the Nord theme.

    .venv/bin/python tools/screenshots.py --sbp tcp://HOST:55555 [--ubx-capture FILE.ubx]

--sbp is a Swift receiver (use one that outputs simulated data: the screenshots show whatever
it sends).  --ubx-capture is any UBX recording, for the u-blox view.  The receiver's real
address is replaced in the pictures by 192.0.2.10 (a documentation address), and the NTRIP
views use tools/fake_ntrip.py on this machine, so no real caster, account or address appears.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import asyncio
import dataclasses
import shutil
import tempfile

from gpsview.app import GpsView
from gpsview.corrections import parse_sink
from gpsview.ntrip import NtripConfig
from gpsview.swift import sbp_feed
from gpsview.transports import parse_endpoint
from gpsview.ubx import ubx_feed
from tools.fake_ntrip import FakeCaster

SIZE = (100, 40)
OUT = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                    "docs", "screenshots")


async def wait_for(cond, tries=400, step=0.05) -> bool:
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(step)
    return False


def save(app: GpsView, name: str) -> None:
    path = _os.path.join(OUT, name)
    with open(path, "w") as fh:
        fh.write(app.export_screenshot(title="gpsview"))
    print("wrote", path)


def disguised(feed, label: str):
    """The same feed, labelled with a documentation address instead of the real one."""
    return dataclasses.replace(feed, label=label)


async def sbp_shots(spec: str) -> None:
    ep = parse_endpoint(spec, "sbp")
    feed = disguised(sbp_feed(ep), "tcp://192.0.2.10:55555")
    caster = FakeCaster(rate=0.1)
    cport = await caster.start()
    side = bytearray()

    async def sink_port(reader, writer):                 # stands in for the receiver's RTCM port
        while data := await reader.read(65536):
            side.extend(data)
    server = await asyncio.start_server(sink_port, "127.0.0.1", 0)
    sport = server.sockets[0].getsockname()[1]
    try:
        app = GpsView(feed)
        async with app.run_test(size=SIZE) as pilot:
            assert await wait_for(lambda: app.gps.fix.mode >= 2 and app.gps.sats), "no data"
            await pilot.pause(3)
            save(app, "gnss.svg")

            await pilot.press("o")                       # the input dialog, set up for a tty
            await pilot.pause(0.3)
            app.screen.query_one("#src_proto").value = "ubx"
            await pilot.pause(0.3)
            app.screen.query_one("#src_spec").value = "/dev/ttyUSB0"
            app.screen.query_one("#src_baud").value = "115200"
            await pilot.pause(0.3)
            save(app, "input-dialog.svg")
            await pilot.press("escape")
            await pilot.pause(0.3)

            await pilot.press("r")                       # the RTK dialog, filled in, not started
            await pilot.pause(0.3)
            app.screen.query_one("#ntrip_url").value = "caster.example.com:2101/MOUNT"
            app.screen.query_one("#ntrip_user").value = "rover"
            app.screen.query_one("#ntrip_gga").value = "10"
            await pilot.pause(0.3)
            save(app, "rtk-dialog.svg")
            await pilot.press("escape")
            await pilot.pause(0.3)

            cfg = NtripConfig("127.0.0.1", cport, "RTCM")
            app.start_ntrip(cfg, parse_sink(f"tcp://127.0.0.1:{sport}", "sbp", ep))
            assert await wait_for(lambda: app.ntrip and app.ntrip.frames_in > 20)
            await pilot.press("3")
            await pilot.pause(2.5)
            save(app, "rtk.svg")
    finally:
        server.close()
        await caster.stop()


async def ubx_shot(path: str) -> None:
    tmp = tempfile.mkdtemp()
    try:
        name = _os.path.join(tmp, "zed-f9p.ubx")
        shutil.copy(path, name)
        ep = dataclasses.replace(parse_endpoint(name, "ubx"), rate=0)
        feed = ubx_feed(ep)
        app = GpsView(disguised(feed, "file://zed-f9p.ubx"))
        async with app.run_test(size=SIZE) as pilot:
            assert await wait_for(lambda: app.gps.fix.mode >= 2 and len(app.gps.sats) > 10)
            await pilot.pause(2)
            app.screen.dismiss("quit") if app.screen is not app.screen_stack[0] else None
            await pilot.pause(0.5)
            save(app, "ubx.svg")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main(args) -> None:
    _os.makedirs(OUT, exist_ok=True)
    if args.sbp:
        await sbp_shots(args.sbp)
    if args.ubx_capture:
        await ubx_shot(args.ubx_capture)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sbp", metavar="SOURCE", help="a Swift receiver, e.g. tcp://HOST:55555")
    ap.add_argument("--ubx-capture", metavar="FILE", help="a UBX recording for the u-blox view")
    a = ap.parse_args()
    if not (a.sbp or a.ubx_capture):
        ap.error("give --sbp and/or --ubx-capture")
    asyncio.run(main(a))
