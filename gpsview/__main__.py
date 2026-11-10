"""Command line:  python -m gpsview [server[:port[:device]]]   (or ./gpsview)."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import re
import sys

from . import __version__, views
from .app import GpsView
from .fmt import DD, DDMM, DDMMSS
from .ntrip import NtripError, fetch_sourcetable, make_setup, parse_ntrip_url
from .sources import make_feed
from .transports import FILE_RATE

LL_FORMATS = {"d": DD, "m": DDMM, "s": DDMMSS}

EPILOG = """\
The default server is localhost:2947.  A device name after the port limits the
watch to that device, as in  gpsview localhost:2947:/dev/ttyACM0.

Without gpsd:  -n SOURCE reads raw NMEA 0183,  -sbp SOURCE reads Swift Binary Protocol,
-ubx SOURCE reads u-blox UBX.
SOURCE is  /dev/ttyUSB0[:baud],  [tcp://]host:port,  udp://[host]:port,  or  a file
(file:///path or a path to a regular file), which is replayed at serial speed.
On a tty the baud rate is detected by listening unless you give one (/dev/ttyUSB0:115200
or -b 115200); the protocol's usual rate (NMEA 9600, SBP 115200, UBX 38400) is tried first.

RTK corrections (off unless asked for): --ntrip [ntrip://|ntrips://][user[:pass]@]host[:port]/MOUNT
pulls RTCM from an NTRIP caster and writes it to the receiver, on the data link ("same", the
default) or on another interface with --rtcm-out /dev/ttyUSB1[:baud] | host:port |
udp://host:port.  Credentials: the URL, --ntrip-user, or NTRIP_USER and NTRIP_PASSWORD (the
environment keeps the password out of ps and shell history).  --ntrip-list HOST[:PORT] shows
a caster's mountpoints.  --ntrip-gga SECONDS sends your position to the caster (VRS networks
need it); nothing about your position is sent without it.  gpsview never sends anything else
to a receiver: no configuration commands.

You can also change the input from inside the app: press o (or choose "Configure another
input" when the source cannot be reached) to pick the protocol, source and baud rate.

Keys (same as cgps): h help, q quit, o input, d lat/lon format, i/m/n units, t true or
magnetic track, s raw messages on or off, w log raw data to a file on or off,
c clear the raw pane, 1/2/3 GNSS/IMU/RTK tabs.

The raw data log is named gpsview_<protocol>_yyyymmdd_hhmmZ.log (UTC) and goes in the
directory you launch from, unless --log-dir says otherwise.
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gpsview",
        description="Show the fix, satellites and raw data from a gpsd server or a raw NMEA, SBP or UBX source (a Textual clone of cgps).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("server", nargs="?", metavar="server[:port[:device]]",
                    help="gpsd host, port and optional device (default localhost:2947)")
    ap.add_argument("-n", "--nmea", metavar="SOURCE",
                    help="read raw NMEA 0183 from a tty, TCP or UDP, bypassing gpsd")
    ap.add_argument("-sbp", "--sbp", metavar="SOURCE",
                    help="read Swift Binary Protocol from a tty, TCP, UDP or file, bypassing gpsd")
    ap.add_argument("-ubx", "--ubx", metavar="SOURCE",
                    help="read u-blox UBX from a tty, TCP, UDP or file, bypassing gpsd")
    ap.add_argument("-b", "--baud", metavar="RATE", default=None,
                    help="baud rate for a tty source; the same as /dev/ttyUSB0:RATE.  Without "
                         "one, gpsview listens at the usual rates and picks the one that "
                         "works (it sends nothing); 'auto' says so explicitly")
    ap.add_argument("--file-rate", type=float, default=FILE_RATE, metavar="BYTES_PER_S",
                    help="replay speed for a file source (default %(default)g, 115200 baud; "
                         "0 = as fast as possible)")
    ap.add_argument("--ntrip", metavar="URL",
                    help="get RTCM corrections from this NTRIP caster and send them to the "
                         "receiver (see --rtcm-out)")
    ap.add_argument("--rtcm-out", metavar="DEST", default=None,
                    help="where corrections go: 'same' (the data link; the default), "
                         "/dev/ttyX[:baud], [tcp://]host:port, or udp://host:port")
    ap.add_argument("--ntrip-user", metavar="USER", default=None,
                    help="caster user name (password: NTRIP_PASSWORD in the environment)")
    ap.add_argument("--ntrip-version", type=int, choices=[1, 2], default=2,
                    help="NTRIP protocol version (default 2; 1 for old casters)")
    ap.add_argument("--ntrip-gga", type=float, default=0.0, metavar="SECONDS",
                    help="send your position to the caster as GGA this often (0 = never, the "
                         "default; VRS mountpoints need about 10)")
    ap.add_argument("--ntrip-list", metavar="HOST[:PORT]", default=None,
                    help="list a caster's mountpoints and exit")
    ap.add_argument("-i", "--imu", action="store_true", help="start on the IMU tab")
    ap.add_argument("-r", "--rtk", action="store_true", help="start on the RTK tab")
    ap.add_argument("-l", "--llfmt", choices=sorted(LL_FORMATS), default="d",
                    help="lat/lon format: d = DD.dddddddd, m = DD MM.mmmmmm', "
                         "s = DD MM' SS.sssss\"")
    ap.add_argument("-m", "--magtrack", action="store_true",
                    help="show the track as the estimated magnetic track")
    ap.add_argument("-s", "--silent", action="store_true",
                    help="start with the raw messages off (toggle with s)")
    ap.add_argument("--log", action="store_true",
                    help="start logging the raw data to a file (toggle with w)")
    ap.add_argument("--log-dir", metavar="DIR", default=None,
                    help="directory for log files (default: the directory you launch from)")
    ap.add_argument("-u", "--units", choices=sorted(views.UNITS), default=None,
                    help="i = imperial, m = metric, n = nautical "
                         "(default: from GPSD_UNITS or the locale, as cgps)")
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return ap


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _list_mountpoints(ap: argparse.ArgumentParser, args) -> int:
    """--ntrip-list: print what a caster offers.  The caster's text is data from the network, so
    control characters are removed before it reaches the terminal."""
    try:
        cfg = parse_ntrip_url(args.ntrip_list, user=args.ntrip_user, need_mountpoint=False)
        cfg = dataclasses.replace(cfg, version=args.ntrip_version, mountpoint="")
        streams = asyncio.run(fetch_sourcetable(cfg))
    except ValueError as exc:
        ap.error(str(exc))
    except (NtripError, OSError) as exc:
        print(f"gpsview: {exc}", file=sys.stderr)
        return 1
    if not streams:
        print(f"{cfg.label}: the sourcetable has no streams", file=sys.stderr)
        return 1
    rows = [("MOUNTPOINT", "FORMAT", "SYSTEMS", "LAT", "LON", "VRS", "AUTH", "BPS")] + [
        (s.mountpoint, s.format, s.nav_system, f"{s.latitude:.2f}", f"{s.longitude:.2f}",
         "gga" if s.nmea else "", {"N": "none", "B": "basic", "D": "digest"}.get(
             s.authentication, s.authentication), str(s.bitrate or "")) for s in streams]
    rows = [tuple(_CONTROL.sub("?", c) for c in r) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def _ntrip_setup(args, endpoint, protocol: str):
    """The NTRIP config and where the corrections go, checked before the app starts."""
    return make_setup(args.ntrip, args.rtcm_out, endpoint=endpoint, protocol=protocol,
                      user=args.ntrip_user, version=args.ntrip_version, gga=args.ntrip_gga)


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.ntrip_list is not None:
        return _list_mountpoints(ap, args)
    if args.ntrip is None:
        stray = [o for o, v in (("--rtcm-out", args.rtcm_out), ("--ntrip-user", args.ntrip_user),
                                ("--ntrip-gga", args.ntrip_gga or None)) if v is not None]
        if stray:
            ap.error(f"{', '.join(stray)} only makes sense with --ntrip")
    chosen = [name for name, given in (("server", args.server is not None),
                                       ("-n", args.nmea is not None),
                                       ("-sbp", args.sbp is not None),
                                       ("-ubx", args.ubx is not None)) if given]
    if len(chosen) > 1:
        ap.error(f"choose one source: {', '.join(chosen)} given")
    ntrip = None
    try:
        if args.file_rate < 0:
            raise ValueError("--file-rate must be 0 or more")
        protocol, spec = next(((p, v) for p, v in (("nmea", args.nmea), ("sbp", args.sbp),
                                                   ("ubx", args.ubx)) if v is not None),
                              ("gpsd", args.server or ""))
        source = make_feed(protocol, spec, baud=args.baud, file_rate=args.file_rate)
        if args.ntrip is not None:
            ntrip = _ntrip_setup(args, source.endpoint, protocol)
    except ValueError as exc:
        ap.error(str(exc))
    # cgps: IMU wins over RTK if both are given
    tab = "imu" if args.imu else "rtk" if args.rtk else "gnss"
    units = args.units or views.default_units()
    log_dir = args.log_dir or os.getcwd()
    if not os.path.isdir(log_dir):
        ap.error(f"--log-dir {log_dir}: not a directory")
    app = GpsView(source, units=units, deg=LL_FORMATS[args.llfmt],
                  magnetic=args.magtrack, raw_on=not args.silent, tab=tab,
                  log_dir=log_dir, log_at_start=args.log, ntrip=ntrip)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
