"""Simulated receivers for NMEA and SBP.  For demos and tests; no hardware needed.

    python3 tools/fake_feeds.py nmea --tcp 10110      # NMEA over TCP
    python3 tools/fake_feeds.py nmea --udp 10111      # NMEA datagrams to 127.0.0.1:10111
    python3 tools/fake_feeds.py sbp  --pty            # SBP on a pty; prints the tty name
    python3 tools/fake_feeds.py sbp  --no-fix --tcp 10112
    python3 tools/fake_feeds.py ubx  --tcp 10113      # UBX mixed with NMEA, as a u-blox sends

Then:  bin/gpsview -n tcp://127.0.0.1:10110     or   bin/gpsview -sbp tcp://127.0.0.1:10112
       bin/gpsview -ubx tcp://127.0.0.1:10113

NMEA: one epoch per --rate seconds: GGA, RMC, VTG, GSA, GSV (GPS and GLONASS), GST, ZDA,
with correct checksums.  SBP: UTC_TIME, POS_LLH, VEL_NED, DOPS, SV_AZ_EL,
MEASUREMENT_STATE, BASELINE_NED, built with the sbp package's own encoder.  Values are
invented but in range and use the spec's units.  UBX: NAV-PVT, NAV-DOP, NAV-SAT, NAV-SIG,
NAV-RELPOSNED, with a GGA sentence in between, as a u-blox on its default port sends.
"""
from __future__ import annotations

import os as _os
import sys as _sys

# Run as  python tools/fake_x.py : the project root (where the gpsview package is) is not on
# sys.path then, only this directory.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import asyncio
import math
import os
import time
from datetime import datetime, timezone
from typing import Callable

import struct
import tty

from gpsview import ubx
from gpsview.nmea import make_sentence
from sbp.navigation import (MsgBaselineNED, MsgDops, MsgPosLLH, MsgUtcTime,
                            MsgVelNED)
from sbp.observation import MsgSvAzEl
from sbp.tracking import MsgMeasurementState

LAT0, LON0 = 37.7749, -122.4194
# satellites as (PRN, elevation, azimuth, SNR in dB-Hz)
GPS_SATS = [(1, 60, 83, 46), (3, 40, 308, 41), (7, 25, 344, 39), (9, 70, 228, 45),
            (12, 15, 120, 40), (14, 45, 10, 44), (17, 35, 200, 35), (22, 55, 300, 42)]
GLO_SATS = [(65, 30, 100, 38), (66, 45, 200, 41)]
SBP_SATS = GPS_SATS[:4]


def _position(now: float, has_fix: bool) -> tuple[float, float, float]:
    phase = now / 20.0
    lat = LAT0 + 0.0005 * math.sin(phase)
    lon = LON0 + 0.0005 * math.cos(phase)
    speed = 1.2 + 0.3 * math.sin(now / 7.0)
    return lat, lon, speed if has_fix else 0.0


def _nmea_lat(lat: float) -> str:
    deg = int(abs(lat))
    return f"{deg:02d}{(abs(lat) - deg) * 60:07.4f},{'N' if lat >= 0 else 'S'}"


def _nmea_lon(lon: float) -> str:
    deg = int(abs(lon))
    return f"{deg:03d}{(abs(lon) - deg) * 60:07.4f},{'E' if lon >= 0 else 'W'}"


def nmea_epoch(now: float, has_fix: bool = True) -> bytes:
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    hms = f"{t:%H%M%S}.00"
    ddmmyy = f"{t:%d%m%y}"
    lat, lon, speed_mps = _position(now, has_fix)
    knots = speed_mps / 0.514444
    track = (now * 5.0) % 360.0
    q = 1 if has_fix else 0
    pos_gga = f"{_nmea_lat(lat)},{_nmea_lon(lon)}" if has_fix else ",,,"
    pos_rmc = f"{_nmea_lat(lat)},{_nmea_lon(lon)}" if has_fix else ",,,"
    lines = [
        f"GPGGA,{hms},{pos_gga},{q},08,0.9,{42.5:.1f},M,-34.0,M,," if has_fix
        else f"GPGGA,{hms},{pos_gga},0,00,99.9,,M,,M,,",
        f"GPRMC,{hms},{'A' if has_fix else 'V'},{pos_rmc},{knots:.2f},{track:.1f},"
        f"{ddmmyy},003.1,W,A",
        f"GPVTG,{track:.1f},T,,M,{knots:.2f},N,{speed_mps * 3.6:.2f},K,A",
        f"GPGSA,A,{3 if has_fix else 1},01,03,07,09,12,14,17,22,,,,,1.2,0.9,1.0",
    ]
    lines += _gsv("GP", GPS_SATS)
    lines += _gsv("GL", GLO_SATS)
    lines += [
        f"GPGST,{hms},1.8,0.0,0.0,0.0,1.6,1.9,3.2",
        f"GPZDA,{hms},{t:%d},{t:%m},{t:%Y}",
    ]
    return b"".join(make_sentence(line) for line in lines)


def _gsv(talker: str, sats: list[tuple[int, int, int, int]]) -> list[str]:
    out = []
    chunks = [sats[i:i + 4] for i in range(0, len(sats), 4)]
    for n, chunk in enumerate(chunks, start=1):
        body = f"{talker}GSV,{len(chunks)},{n},{len(sats)}"
        for prn, el, az, snr in chunk:
            body += f",{prn:02d},{el},{az},{snr}"
        out.append(body)
    return out


# ----------------------------------------------------------------- SBP
def sbp_epoch(now: float, has_fix: bool = True) -> bytes:
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    tow = int((now % 604800) * 1000)
    lat, lon, speed = _position(now, has_fix)
    frames = [
        MsgUtcTime(sender=1, flags=0x11 if has_fix else 0, tow=tow, year=t.year,
                   month=t.month, day=t.day, hours=t.hour, minutes=t.minute,
                   seconds=t.second, ns=t.microsecond * 1000),
        MsgPosLLH(sender=1, tow=tow, lat=lat, lon=lon, height=42.5, h_accuracy=18,
                  v_accuracy=31, n_sats=8, flags=6 if has_fix else 0),
        MsgVelNED(sender=1, tow=tow, n=int(speed * 1000 * 0.7), e=int(speed * 1000 * 0.7),
                  d=-20, h_accuracy=10, v_accuracy=20, n_sats=8, flags=2),
        MsgDops(sender=1, tow=tow, gdop=200, pdop=160, tdop=90, hdop=105, vdop=120,
                flags=1 if has_fix else 0),
        MsgSvAzEl(sender=1, azel=[{"sid": {"sat": prn, "code": 0}, "az": az // 2, "el": el}
                                  for prn, el, az, _ in SBP_SATS]),
        # cn0 is in units of dB-Hz / 4
        MsgMeasurementState(sender=1, states=[
            {"mesid": {"sat": prn, "code": 0}, "cn0": snr * 4}
            for prn, _, _, snr in SBP_SATS] if has_fix else []),
    ]
    if has_fix:
        frames.append(MsgBaselineNED(sender=1, tow=tow, n=1234, e=-567, d=89,
                                     h_accuracy=10, v_accuracy=20, n_sats=8, flags=4))
    return b"".join(f.to_binary() for f in frames)


def ubx_epoch(now: float, has_fix: bool = True) -> bytes:
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    tow = int((now % 604800) * 1000)
    lat, lon, speed = _position(now, has_fix)
    # NAV-PVT, 92 bytes: fixType 3, flags carrSoln = 2 (RTK fixed) bits 6-7, diffSoln bit 1
    pvt = struct.pack(
        "<IHBBBBBBIiBBBBiiiiIIiiiiiIIH", tow, t.year, t.month, t.day, t.hour, t.minute,
        t.second, 0x07 if has_fix else 0, 20, t.microsecond * 1000, 3 if has_fix else 0,
        0x83 if has_fix else 0, 0, 22, int(lon * 1e7), int(lat * 1e7), 42500, 12000, 18, 31,
        int(speed * 700), int(speed * 700), -20, int(speed * 1000), 4500000, 120, 150000, 160)
    pvt += bytes(92 - len(pvt))
    dop = struct.pack("<IHHHHHHH", tow, 200, 160, 90, 120, 105, 80, 70)
    ubx_sats = [(0, 1, 60, 83, 46), (0, 3, 40, 308, 41), (2, 5, 25, 344, 39),
                (6, 1, 70, 228, 45), (3, 8, 15, 120, 40)]      # gnssId, svId, el, az, cno
    sat = struct.pack("<IBBH", tow, 1, len(ubx_sats), 0) + b"".join(
        struct.pack("<BBBbhhI", g, sv, cno, el, az, 0, 0x08 | 0x10 | 7)
        for g, sv, el, az, cno in ubx_sats)
    sig = struct.pack("<IBBH", tow, 0, len(ubx_sats), 0) + b"".join(
        struct.pack("<BBBBhBBBBHI", g, sv, 0, 0, 0, cno, 7, 0, 0, 0x08 | 1, 0)
        for g, sv, el, az, cno in ubx_sats)
    rel = bytearray(40)
    rel[2:4] = struct.pack("<H", 12)
    rel[4:8] = struct.pack("<I", tow)
    rel[8:20] = struct.pack("<iii", 123, -56, 9)
    rel[36:40] = struct.pack("<I", 0x01 | 0x02 | 0x04 | (2 << 3))      # fixed RTK
    out = [ubx.make_frame(0x01, 0x07, pvt), ubx.make_frame(0x01, 0x04, dop),
           ubx.make_frame(0x01, 0x35, sat), ubx.make_frame(0x01, 0x43, sig)]
    if has_fix:
        out.append(ubx.make_frame(0x01, 0x3C, bytes(rel)))
    out.append(nmea_epoch(now, has_fix).split(b"\r\n")[0] + b"\r\n")      # a u-blox mixes NMEA in
    return b"".join(out)


def make_payload(protocol: str, has_fix: bool) -> Callable[[float], bytes]:
    if protocol == "nmea":
        return lambda now: nmea_epoch(now, has_fix)
    if protocol == "ubx":
        return lambda now: ubx_epoch(now, has_fix)
    return lambda now: sbp_epoch(now, has_fix)


# ----------------------------------------------------------------- servers
class FakeFeed:
    """Sends make(now) every rate seconds over TCP (server), UDP (datagrams) or a pty."""

    def __init__(self, make: Callable[[float], bytes], rate: float = 1.0) -> None:
        self.make = make
        self.rate = rate
        self.stalled = False                 # True: keep the link, send nothing
        self.received = bytearray()          # what the app wrote to us (RTCM corrections)
        self.port: int | None = None
        self.tty: str | None = None
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._fds: list[int] = []

    async def tcp(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def _client(self, reader, writer) -> None:
        self._writers.add(writer)
        task = asyncio.create_task(self._stream_tcp(writer))
        self._tasks.add(task)
        try:
            while True:                      # the app may write corrections to us
                data = await reader.read(65536)
                if not data:                 # the client went away
                    break
                self.received += data
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            task.cancel()
            self._writers.discard(writer)
            writer.close()

    async def _stream_tcp(self, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await asyncio.sleep(self.rate)
                if not self.stalled:
                    writer.write(self.make(time.time()))
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    def udp(self, port: int, host: str = "127.0.0.1") -> None:
        """Send datagrams to host:port (the app is the one listening)."""
        self._tasks.add(asyncio.create_task(self._stream_udp(host, port)))

    async def _stream_udp(self, host: str, port: int) -> None:
        loop = asyncio.get_running_loop()
        received = self.received

        class Receiver(asyncio.DatagramProtocol):     # replies come back to our own port
            def datagram_received(self, data, addr):
                received.extend(data)

        transport, _ = await loop.create_datagram_endpoint(Receiver, remote_addr=(host, port))
        try:
            while True:
                await asyncio.sleep(self.rate)
                if not self.stalled:
                    transport.sendto(self.make(time.time()))
        except asyncio.CancelledError:
            pass
        finally:
            transport.close()

    def pty(self) -> str:
        """A pty pair.  Returns the slave's name; the app opens it as a serial port."""
        master, slave = os.openpty()
        tty.setraw(slave)                    # no echo: what we read from master is what the app wrote
        os.set_blocking(master, False)
        self._fds = [master, slave]
        self.tty = os.ttyname(slave)
        self._tasks.add(asyncio.create_task(self._stream_pty(master)))
        return self.tty

    async def _stream_pty(self, master: int) -> None:
        last = 0.0
        try:
            while True:
                await asyncio.sleep(min(self.rate, 0.05))
                try:
                    self.received += os.read(master, 65536)
                except (BlockingIOError, OSError):
                    pass
                if not self.stalled and time.time() - last >= self.rate:
                    last = time.time()
                    os.write(master, self.make(last))
        except asyncio.CancelledError:
            pass

    def drop_connections(self) -> None:
        for w in list(self._writers):
            w.close()

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self.drop_connections()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for fd in self._fds:
            os.close(fd)
        self._fds = []


async def _main(args: argparse.Namespace) -> None:
    feed = FakeFeed(make_payload(args.protocol, not args.no_fix), rate=args.rate)
    if args.tcp is not None:
        port = await feed.tcp(args.host, args.tcp)
        print(f"fake {args.protocol} over TCP on {args.host}:{port} (Ctrl-C to stop)", flush=True)
    elif args.udp is not None:
        feed.udp(args.udp, args.host)
        print(f"fake {args.protocol} datagrams to {args.host}:{args.udp} (Ctrl-C to stop)",
              flush=True)
    else:
        print(f"fake {args.protocol} on {feed.pty()} (Ctrl-C to stop)", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser(description="simulated NMEA, SBP or UBX receiver for gpsview")
    ap.add_argument("protocol", choices=["nmea", "sbp", "ubx"])
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--tcp", type=int, metavar="PORT")
    where.add_argument("--udp", type=int, metavar="PORT")
    where.add_argument("--pty", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-fix", action="store_true")
    ap.add_argument("--rate", type=float, default=1.0, help="seconds between epochs")
    args = ap.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
