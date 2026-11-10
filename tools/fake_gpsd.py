"""A simulated gpsd for demos and tests.  It speaks the gpsd JSON protocol on TCP.

    python3 tools/fake_gpsd.py                  # port 2947, 3D fix moving in a small circle
    python3 tools/fake_gpsd.py --no-fix         # NO FIX, no position
    python3 tools/fake_gpsd.py --port 2950      # another port

Then:  ./gpsview localhost:2947   (or  ./gpsview localhost:2950)

It sends VERSION on connect, then once per --rate seconds TPV, SKY and ATT after a
?WATCH.  The values are invented but have the right types and ranges.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from datetime import datetime, timezone

API_MAJOR, API_MINOR = 16, 1

SATS = [
    # gnssid, svid, sigid, PRN, el, az
    (0, 5, 0, 5, 71, 152), (0, 12, 0, 12, 34, 40), (0, 18, 0, 18, 58, 281),
    (0, 25, 0, 25, 12, 320), (0, 27, 0, 27, 46, 96), (0, 31, 0, 31, 9, 210),
    (2, 11, 1, 311, 52, 15), (2, 19, 1, 319, 23, 244),
    (3, 6, 1, 36, 38, 110), (6, 3, 0, 67, 27, 330),
    (1, 133, 0, 133, 15, 77),
]


class FakeGpsd:
    def __init__(self, *, has_fix: bool = True, rate: float = 1.0,
                 lat: float = 37.7749, lon: float = -122.4194,
                 device: str = "/dev/ttyFAKE") -> None:
        self.has_fix = has_fix
        self.rate = rate
        self.lat0, self.lon0 = lat, lon
        self.device = device
        self.stalled = False           # True: keep the socket open, send nothing
        self.port: int | None = None
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()

    # --- lifecycle -----------------------------------------------------
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        self.drop_connections()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def drop_connections(self) -> None:
        """Close every client socket (simulates gpsd or the link going away)."""
        for w in list(self._writers):
            w.close()
        for t in list(self._tasks):
            t.cancel()

    # --- protocol ------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            writer.write(self._line({"class": "VERSION", "release": "3.27.6~dev",
                                     "rev": "fake", "proto_major": API_MAJOR,
                                     "proto_minor": API_MINOR}))
            await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                cmd = raw.decode("ascii", "replace").strip()
                if cmd.startswith("?WATCH="):
                    task = asyncio.create_task(self._stream(writer))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _stream(self, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await asyncio.sleep(self.rate)
                if self.stalled:
                    continue
                for obj in self.messages(time.time()):
                    writer.write(self._line(obj))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    def _line(self, obj: dict) -> bytes:
        return (json.dumps(obj, separators=(",", ":")) + "\r\n").encode("ascii")

    # --- data ----------------------------------------------------------
    def messages(self, now: float) -> list[dict]:
        t = datetime.fromtimestamp(now, tz=timezone.utc)
        stamp = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
        phase = now / 20.0
        lat = self.lat0 + 0.0005 * math.sin(phase)
        lon = self.lon0 + 0.0005 * math.cos(phase)
        speed = 1.2 + 0.3 * math.sin(now / 7.0)
        track = (now * 5.0) % 360.0
        mode = 3 if self.has_fix else 1
        msgs: list[dict] = []

        tpv: dict = {"class": "TPV", "device": self.device, "time": stamp,
                     "mode": mode, "status": 1 if self.has_fix else 0,
                     "leapseconds": 18}
        if self.has_fix:
            tpv.update({
                "lat": round(lat, 9), "lon": round(lon, 9),
                "altHAE": 42.5 + 0.2 * math.sin(now), "altMSL": 12.1 + 0.2 * math.sin(now),
                "speed": round(speed, 3), "track": round(track, 4),
                "climb": round(0.05 * math.cos(now / 3.0), 3),
                "epx": 1.8, "epy": 1.6, "epv": 3.1, "eph": 2.4, "sep": 4.2,
                "epd": 3.0, "eps": 0.12, "ept": 0.005,
                "ecefx": -2706000.1, "ecefy": -4261000.4, "ecefz": 3885000.9,
                "ecefvx": 0.1, "ecefvy": -0.2, "ecefvz": 0.05,
            })
            if int(now) % 30 < 10:    # RTK base link for part of the cycle
                tpv.update({"baseS": 3, "baseE": 0.4321, "baseN": -1.2345, "baseU": 0.0102,
                            "baseL": 1.3, "baseC": 110.5, "dgpsRatio": 4.5,
                            "dgpsAge": 0.8, "dgpsSta": 1022})
        msgs.append(tpv)

        sats = []
        for gnssid, svid, sigid, prn, el, az in SATS:
            sats.append({"PRN": prn, "gnssid": gnssid, "svid": svid, "sigid": sigid,
                         "el": el, "az": az,
                         "ss": round(30 + (prn * 7 % 18), 1),
                         "used": self.has_fix and el > 15,
                         "health": 1})
        msgs.append({"class": "SKY", "device": self.device, "time": stamp,
                     "xdop": 0.82, "ydop": 0.71, "vdop": 1.2, "hdop": 1.05,
                     "pdop": 1.6, "tdop": 0.9, "gdop": 2.0,
                     "nSat": len(sats), "uSat": sum(1 for s in sats if s["used"]),
                     "satellites": sats})
        msgs.append({"class": "ATT", "device": self.device, "time": stamp,
                     "heading": round(track, 3), "pitch": 1.2, "roll": -0.4,
                     "dip": 53.5, "mag_st": "N", "pitch_st": "N", "roll_st": "N"})
        # an IMU (sensor) report, as a u-blox ESF-RAW message would give
        msgs.append({"class": "IMU", "device": self.device, "time": stamp,
                     "msg": "UBX-ESF-RAW", "timeTag": int(now * 1000) % 4294967296,
                     "acc_x": 0.12 * math.sin(now), "acc_y": -0.05, "acc_z": 9.79,
                     "gyro_temp": 24.5, "gyro_x": 0.4 * math.cos(now), "gyro_y": 0.1,
                     "gyro_z": -0.2, "mag_x": 2454.0, "mag_y": -511.5, "mag_z": 12.0})
        return msgs


async def _main(args: argparse.Namespace) -> None:
    gps = FakeGpsd(has_fix=not args.no_fix, rate=args.rate)
    port = await gps.start(args.host, args.port)
    print(f"fake gpsd listening on {args.host}:{port} (Ctrl-C to stop)", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser(description="simulated gpsd for gpsview")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2947)
    ap.add_argument("--no-fix", action="store_true", help="report NO FIX and no position")
    ap.add_argument("--rate", type=float, default=1.0, help="seconds between reports")
    args = ap.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
