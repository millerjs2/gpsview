"""Number and text formatting, ported from gpsd (libgps/gpsdclient.c) and cgps (clients/cgps.c).

The C printf formats are kept as Python % formats so the output lines up with cgps.
tests/golden_fmt.tsv holds the output of the original C code for a set of inputs
(regenerate it with tools/golden/gen_golden.sh), and tests/test_fmt.py checks
every case against it.
"""
from __future__ import annotations

import math
import struct

# gps.h: unit conversions (exact, from the definitions there)
FEET_TO_METERS = 0.3048
METERS_TO_FEET = 1 / FEET_TO_METERS
MPS_TO_MPH = 1 / 0.44704
MPS_TO_KNOTS = 3600.0 / 1852.0
MPS_TO_KPH = 3.6

# deg_str_type
DD, DDMM, DDMMSS = "dd", "ddmm", "ddmmss"

# cgps keeps its unit factors in C 'float', not 'double'. Use the same rounding
# so that the displayed numbers match.
def f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def _finite(x: float) -> bool:
    return math.isfinite(x)


def deg_to_str(kind: str, f: float, suffix_pos: str | None = None,
               suffix_neg: str | None = None) -> str:
    """Latitude/longitude text: DD.dddddddd, DD MM.mmmmmm', or DD MM' SS.sssss".

    Mirrors gpsd's deg_to_str2(). 'n/a' for NaN, inf, or |f| > 360.
    """
    if not _finite(f) or abs(f) > 360.0:
        return "n/a"

    suffix = ""
    if f < 0.0:
        f = -f
        if suffix_neg is not None:
            suffix = suffix_neg
    elif suffix_pos is not None:
        suffix = suffix_pos

    # round to the displayed precision
    if kind == DDMM:
        f += (0.5 * 1e-6) / 60
    elif kind == DDMMSS:
        f += (0.5 * 1e-5) / 3600
    else:
        f += 0.5 * 1e-8

    fmin, fdeg = math.modf(f)
    deg = int(fdeg)
    if deg == 360:                   # rounded up to a full turn
        deg = 0
        fmin = 0.0

    if kind == DD:
        frac_deg = int(fmin * 100000000.0)
        return f"{deg:3d}.{frac_deg:08d}{suffix}"

    fsec, minf = math.modf(fmin * 60)
    minute = int(minf)
    if kind == DDMM:
        sec = int(fsec * 1000000.0)
        return "%3d %02d.%06d'%s" % (deg, minute, sec, suffix)

    fdsec, secf = math.modf(fsec * 60.0)
    sec = int(secf)
    dsec = int(fdsec * 100000.0)
    return "%3d %02d' %02d.%05d\"%s" % (deg, minute, sec, dsec, suffix)


def maidenhead(lat: float, lon: float) -> str:
    """Maidenhead (grid square) locator, 5 pairs, as gpsd's maidenhead()."""
    if 180.001 < abs(lon) or 90.001 < abs(lat):
        return "    n/a "

    buf = [""] * 10

    if 179.99999 < lon:
        lon = 179.99999
    lon += 180.0
    t1 = int(lon / 20)
    buf[0] = chr(t1 + ord("A"))
    if buf[0] > "R":
        buf[0] = "R"
    lon -= float(t1) * 20.0

    t1 = int(lon) // 2
    buf[2] = chr(t1 + ord("0"))
    lon -= float(t1) * 2
    lon *= 60.0

    t1 = int(lon / 5)
    buf[4] = chr(t1 + ord("a"))
    lon -= float(t1 * 5)
    lon *= 60.0

    t1 = int(lon / 30)
    if t1 > 9:
        t1 = 9
    buf[6] = chr(t1 + ord("0"))
    lon -= float(t1 * 30)
    lon *= 1000.0

    t1 = int(lon / 1250)
    buf[8] = chr(t1 + ord("a"))

    if 89.99999 < lat:
        lat = 89.99999
    lat += 90.0
    t1 = int(lat / 10.0)
    buf[1] = chr(t1 + ord("A"))
    if buf[1] > "R":
        buf[1] = "R"
    lat -= float(t1) * 10.0

    buf[3] = chr(int(lat) + ord("0"))
    lat -= int(lat)
    lat *= 60.0

    t1 = int(lat / 2.5)
    buf[5] = chr(t1 + ord("a"))
    lat -= float(t1 * 2.5)
    lat *= 60.0

    t1 = int(lat / 15)
    if t1 > 9:
        t1 = 9
    buf[7] = chr(t1 + ord("0"))
    lat -= float(t1 * 15)
    lat *= 1000.0

    t1 = int(lat / 625)
    buf[9] = chr(t1 + ord("a"))

    return "".join(buf)


def ep_to_str(ep: float, factor: float, units: str) -> str:
    """Error estimate, e.g. '+/-   1.5 ft'. Values of 100 or more lose the decimal."""
    if not _finite(ep):
        return " n/a  "
    val = abs(ep * factor)
    if 100 <= val:
        return "+/-%5d %.5s" % (int(val), units)
    return "+/-%5.1f %.5s" % (val, units)


def dop_to_str(dop: float) -> str:
    if not _finite(dop):
        return " n/a "
    return "%5.2f" % dop


def tenth_to_str(val: float, lo: float, hi: float) -> str:
    """Tenths, 5 characters. '  n/a' outside [lo, hi] or for NaN/inf."""
    if not _finite(val) or val < lo or val > hi:
        return "  n/a"
    return "%5.1f" % val


def int_to_str(val: int, lo: int, hi: int) -> str:
    if val < lo or val > hi:
        return "n/a"
    return "%3d" % val


def ecef_to_str(pos: float, vel: float, alt_factor: float, alt_units: str) -> str:
    """ECEF position and velocity, e.g. '  1234.5678 ft   0.123 ft/s'."""
    if not _finite(pos):
        if not _finite(vel):
            return "             n/a    n/a      "
        return "  n/a % 8.3f %2.2s/s" % (vel * alt_factor, alt_units)
    if not _finite(vel):
        return "% 14.4f %2.2s   n/a       " % (pos * alt_factor, alt_units)
    return "% 14.4f %2.2s % 8.3f %2.2s/s" % (pos * alt_factor, alt_units,
                                             vel * alt_factor, alt_units)
