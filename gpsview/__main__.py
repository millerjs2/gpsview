"""Command line:  python -m gpsview [server[:port[:device]]]   (or ./gpsview)."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, views
from .app import GpsView
from .client import parse_source
from .fmt import DD, DDMM, DDMMSS
from .nmea import nmea_feed
from .swift import SBP_DEFAULT, sbp_feed
from .transports import parse_endpoint

LL_FORMATS = {"d": DD, "m": DDMM, "s": DDMMSS}

EPILOG = """\
The default server is localhost:2947.  A device name after the port limits the
watch to that device, as in  gpsview localhost:2947:/dev/ttyACM0.

Without gpsd:  -n SOURCE reads raw NMEA 0183,  -sbp [SOURCE] reads Swift Binary Protocol.
SOURCE is  /dev/ttyUSB0[:baud],  [tcp://]host:port,  or  udp://[host]:port.
NMEA defaults to 9600 baud on a tty, SBP to 115200 (and to tcp://172.16.0.120:55555).

Keys (same as cgps): h help, q quit, d lat/lon format, i/m/n units, t true or
magnetic track, s raw messages on or off, w log raw data to a file on or off,
c clear the raw pane, 1/2/3 GNSS/IMU/RTK tabs.

The raw data log is named gpsview_<protocol>_yyyymmdd_hhmmZ.log (UTC) and goes in the
directory you launch from, unless --log-dir says otherwise.
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gpsview",
        description="Show the fix, satellites and raw data from a gpsd server (a Textual clone of cgps).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("server", nargs="?", metavar="server[:port[:device]]",
                    help="gpsd host, port and optional device (default localhost:2947)")
    ap.add_argument("-n", "--nmea", metavar="SOURCE",
                    help="read raw NMEA 0183 from a tty, TCP or UDP, bypassing gpsd")
    ap.add_argument("-sbp", nargs="?", const=SBP_DEFAULT, default=None, metavar="SOURCE",
                    help="read Swift Binary Protocol from a tty, TCP or UDP, bypassing gpsd "
                         f"(default {SBP_DEFAULT})")
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


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    chosen = [name for name, given in (("server", args.server is not None),
                                       ("-n", args.nmea is not None),
                                       ("-sbp", args.sbp is not None)) if given]
    if len(chosen) > 1:
        ap.error(f"choose one source: {', '.join(chosen)} given")
    try:
        if args.nmea is not None:
            source = nmea_feed(parse_endpoint(args.nmea, "nmea"))
        elif args.sbp is not None:
            source = sbp_feed(parse_endpoint(args.sbp, "sbp"))
        else:
            source = parse_source(args.server)
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
                  log_dir=log_dir, log_at_start=args.log)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
