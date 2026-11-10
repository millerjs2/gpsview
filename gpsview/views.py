"""Text for each panel, built from GpsState.  Pure functions: no Textual, no I/O.

The field order, labels and column positions follow cgps's update_gps_panel(),
update_imu() and update_rtk_panel().  Where gpsview differs on purpose it says so
in the comment next to the code.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import fmt
from .fmt import f32
from .model import (GNSS_CODES, MODE_2D, MODE_3D, SAT_HEALTH_BAD,
                    STATUS_DGPS, STATUS_DR, STATUS_GNSSDR, STATUS_PPS_FIX,
                    STATUS_RTK_FIX, STATUS_RTK_FLT, STATUS_SIM, STATUS_TIME,
                    Attitude, Base, Fix, GpsState, Sat)


# ----------------------------------------------------------------- options
@dataclass(frozen=True)
class Units:
    letter: str
    alt_factor: float      # C 'float' in cgps, so rounded to 32 bits
    alt_units: str
    speed_factor: float
    speed_units: str


# cgps: -u i (imperial), -u n (nautical), -u m (metric)
UNITS = {
    "i": Units("i", f32(fmt.METERS_TO_FEET), "ft", f32(fmt.MPS_TO_MPH), "mph"),
    "n": Units("n", f32(fmt.METERS_TO_FEET), "ft", f32(fmt.MPS_TO_KNOTS), "knots"),
    "m": Units("m", 1.0, "m", f32(fmt.MPS_TO_KPH), "km/h"),
}


@dataclass(frozen=True)
class ViewOpts:
    units: Units = UNITS["i"]
    deg: str = fmt.DD              # cgps 'd' key cycles DD -> DDMM -> DDMMSS
    magnetic: bool = False         # cgps 't' key: true or magnetic track


def default_units() -> str:
    """cgps's gpsd_units(): GPSD_UNITS, then LC_MEASUREMENT / LANG. Unset means imperial."""
    import os
    env = os.environ.get("GPSD_UNITS", "").lower()
    if env == "imperial":
        return "i"
    if env == "nautical":
        return "n"
    if env == "metric":
        return "m"
    for name in ("LC_MEASUREMENT", "LANG"):
        val = os.environ.get(name, "")
        if val:
            if val[:5].lower() == "en_us" or val in ("C", "POSIX"):
                return "i"
            return "m"
    return "i"


# ----------------------------------------------------------------- helpers
STATUS_TEXT = {
    STATUS_DGPS: "DGPS ",
    STATUS_RTK_FIX: "RTKFIX ",
    STATUS_RTK_FLT: "RTKFLT ",
    STATUS_DR: "DR ",
    STATUS_GNSSDR: "GNSSDR ",
    STATUS_TIME: "FIXED ",
    STATUS_PPS_FIX: "P(Y) ",
    STATUS_SIM: "SIM ",
}


def iso8601(dt: datetime) -> str:
    """'2005-06-08T10:34:48.283Z', rounded to milliseconds as gpsd does."""
    rounded = dt + timedelta(microseconds=500)
    return f"{rounded:%Y-%m-%dT%H:%M:%S}.{rounded.microsecond // 1000:03d}Z"


def _or_na(value: float, fmt_str: str, width: int) -> str:
    if not math.isfinite(value):
        return "n/a".rjust(width)
    return (fmt_str % value).rjust(width)


def time_offset(fix_time: datetime, now: datetime) -> str:
    """Host clock minus GPS time, as cgps prints it: sign (a space or '-'), seconds, nanoseconds."""
    diff = now - fix_time
    ns = (diff.days * 86400 + diff.seconds) * 1_000_000_000 + diff.microseconds * 1000
    sign = " " if ns >= 0 else "-"
    ns = abs(ns)
    sec, nsec = divmod(ns, 1_000_000_000)
    if sec > 10000:
        return "%16d s" % sec
    return "%-16s s" % ("%s%d.%09d" % (sign, sec, nsec))


def fix_status(fix: Fix, online: bool, secs: int) -> str:
    if not online:
        return "OFFLINE"
    mod = STATUS_TEXT.get(fix.status, "")
    if fix.mode == MODE_2D:
        text = "2D %sFIX (%d secs)"
    elif fix.mode == MODE_3D:
        text = "%sSURVEYED (%d secs)" if fix.status == STATUS_TIME else "3D %sFIX (%d secs)"
    else:
        text = "NO %sFIX (%d secs)"
    return text % (mod, secs)


class StateTimer:
    """Seconds since the fix state last changed (cgps's status_timer)."""

    def __init__(self) -> None:
        self._state: int | None = None
        self._since = 0.0

    def seconds(self, state: int, now: float | None = None) -> int:
        now = time.time() if now is None else now
        if state != self._state:
            self._state = state
            self._since = now
        return int(now - self._since)


# ----------------------------------------------------------------- GNSS panel
def gnss_lines(st: GpsState, opts: ViewOpts, *, online: bool, secs: int,
               now: datetime) -> list[str]:
    fix = st.fix
    u = opts.units
    rows: list[str] = []

    def add(text: str) -> None:
        rows.append("  " + text)

    # Fill in the time with the leap seconds, as cgps does.
    scr = iso8601(fix.time) if fix.time else "  n/a"
    leap = "(n/a)" if st.leap_seconds is None else "(%2d)" % st.leap_seconds
    add("Time         %-21s %s" % (scr, leap))

    if fix.mode >= MODE_2D:
        lat = fmt.deg_to_str(opts.deg, fix.latitude, " N", " S")
        lon = fmt.deg_to_str(opts.deg, fix.longitude, " E", " W")
    else:
        lat = lon = "n/a"
    add("Latitude         %-25s" % lat)
    add("Longitude        %-25s" % lon)

    if fix.mode >= MODE_3D:
        buf1 = "        n/a" if not math.isfinite(fix.alt_hae) else "%11.3f" % (fix.alt_hae * u.alt_factor)
        buf2 = "        n/a" if not math.isfinite(fix.alt_msl) else "%11.3f" % (fix.alt_msl * u.alt_factor)
    else:
        buf1 = buf2 = "        n/a"
    add("Alt (HAE, MSL) %-11s,%-11s %2.2s" % (buf1, buf2, u.alt_units))

    if math.isfinite(fix.speed):
        scr = "%8.2f" % (fix.speed * u.speed_factor)
    else:
        scr = "  n/a"
    add("Speed          %-21s%5s " % (scr, u.speed_units))

    mag_str = "(mag, var)" if opts.magnetic else "(true, var)"
    if fix.mode >= MODE_2D and math.isfinite(fix.track):
        if not opts.magnetic or not math.isfinite(fix.magnetic_track):
            buf1 = "%5.1f" % fix.track
        else:
            buf1 = "%5.1f" % fix.magnetic_track
        buf2 = "n/a" if not math.isfinite(fix.magnetic_var) else "%6.1f" % fix.magnetic_var
    else:
        buf1 = buf2 = "n/a"
    add("Track %-14.14s %6.6s,%6.6s    deg" % (mag_str, buf1, buf2))

    if math.isfinite(fix.climb):
        scr = "%8.2f" % (fix.climb * u.alt_factor * 60)
    else:
        scr = "  n/a"
    add("Climb          %-16s %5s/min " % (scr, u.alt_units))

    add("Status          %-26s" % fix_status(fix, online, secs))

    # DOPs and error estimates.  cgps hides these when the window is short; gpsview always
    # shows them and scrolls.
    dop = st.dop
    add("Long Err  (XDOP, EPX)  %s, %-11s" % (fmt.dop_to_str(dop.xdop),
                                             fmt.ep_to_str(fix.epx, u.alt_factor, u.alt_units)))
    add("Lat Err   (YDOP, EPY)  %s, %-11s" % (fmt.dop_to_str(dop.ydop),
                                             fmt.ep_to_str(fix.epy, u.alt_factor, u.alt_units)))
    add("Alt Err   (VDOP, EPV)  %s, %-11s" % (fmt.dop_to_str(dop.vdop),
                                             fmt.ep_to_str(fix.epv, u.alt_factor, u.alt_units)))
    add("2D Err    (HDOP, CEP)  %s, %-11s" % (fmt.dop_to_str(dop.hdop),
                                             fmt.ep_to_str(fix.eph, u.alt_factor, u.alt_units)))
    add("3D Err    (PDOP, SEP)  %s, %-11s" % (fmt.dop_to_str(dop.pdop),
                                             fmt.ep_to_str(fix.sep, u.alt_factor, u.alt_units)))
    add("Time Err  (TDOP)       %-18s" % fmt.dop_to_str(dop.tdop))
    add("Geo Err   (GDOP)       %-18s" % fmt.dop_to_str(dop.gdop))
    # cgps passes the speed factor for EPS (speed error) and for EPD (track error, in degrees).
    # gpsview uses 1.0 for degrees, so EPD is not multiplied by mph/knots/km-h.
    add("Speed Err (EPS)            %-11s " % fmt.ep_to_str(fix.eps, u.speed_factor, u.speed_units))
    add("Track Err (EPD)        %-14s " % fmt.ep_to_str(fix.epd, 1.0, "deg"))

    toff = "  Time offset".ljust(25)
    if fix.time is not None:
        toff += time_offset(fix.time, now)
    rows.append(toff)

    grid = fmt.maidenhead(fix.latitude, fix.longitude) if fix.mode >= MODE_2D else "n/a"
    add("Grid Square             %-18s" % grid)

    e = fix.ecef
    add("ECEF X, VX %-27s" % fmt.ecef_to_str(e.x, e.vx, u.alt_factor, u.alt_units))
    add("ECEF Y, VY %-27s" % fmt.ecef_to_str(e.y, e.vy, u.alt_factor, u.alt_units))
    add("ECEF Z, VZ %-27s" % fmt.ecef_to_str(e.z, e.vz, u.alt_factor, u.alt_units))
    return rows


# ----------------------------------------------------------------- satellites
def _sigid_char(sigid: int) -> str:
    """Signal id as one character.  cgps prints ':' ';' '<' for 10 to 12 (ASCII after '9');
    gpsview uses A, B, C."""
    if 2 <= sigid <= 9:
        return str(sigid)
    if 10 <= sigid <= 12:
        return "ABC"[sigid - 10]
    return " "


def sat_rows(sats: tuple[Sat, ...]) -> list[tuple[str, ...]]:
    """Table rows: GNSS, Sig, PRN, Elev, Azim, SNR, Use.  Used satellites first, then by PRN.

    One row per signal: a satellite tracked on L1 and L2 has two rows.  Sig is the signal
    name when the source gives one (SBP), otherwise gpsd's signal id as cgps shows it.
    """
    rows = []
    for s in sorted(sats, key=lambda s: (not s.used, s.prn, s.sigid, s.signal)):
        if s.svid == 0:
            gnss = "  "
        else:
            gnss = GNSS_CODES.get(s.gnssid, "  ")
        sigid = s.signal or (_sigid_char(s.sigid) if s.svid else " ")
        bad = "u" if s.health == SAT_HEALTH_BAD else " "
        rows.append((
            gnss,
            sigid,
            fmt.int_to_str(s.prn, 1, 462),
            fmt.tenth_to_str(s.elevation, -90.0, 90.0),
            fmt.tenth_to_str(s.azimuth, 0.0, 359.0),
            fmt.tenth_to_str(s.ss, 0.0, 254.0),
            bad + ("Y" if s.used else "N"),
        ))
    return rows


# ----------------------------------------------------------------- IMU / ATT panel
def imu_lines(att: Attitude | None, *, attitude: bool) -> list[str]:
    """One box of the IMU tab. attitude=True for the ATT message, False for IMU."""
    if att is None:
        return ["  no data yet"]
    rows = [
        "  %-14s%s" % ("msg", att.msg or ("ATT" if attitude else "")),
        "  %-14s%s" % ("Time", iso8601(att.time) if att.time else "n/a"),
        "  %-14s%s" % ("timeTag", str(att.time_tag) if att.time_tag else "n/a"),
    ]
    spec = [
        ("Accel X", att.acc_x, "m/s^2"), ("Accel Y", att.acc_y, "m/s^2"),
        ("Accel Z", att.acc_z, "m/s^2"),
        ("Gyro T", att.gyro_temp, "deg C"),
        ("Gyro X", att.gyro_x, "deg/s"), ("Gyro Y", att.gyro_y, "deg/s"),
        ("Gyro Z", att.gyro_z, "deg/s"),
        ("Mag X", att.mag_x, ""), ("Mag Y", att.mag_y, ""), ("Mag Z", att.mag_z, ""),
        ("True Heading", att.heading, "deg"), ("Yaw", att.yaw, "deg"),
        ("Pitch", att.pitch, "deg"), ("Roll", att.roll, "deg"),
    ]
    for label, value, unit in spec:
        rows.append("  %-14s%s %s" % (label, _or_na(value, "% 12.4f", 12), unit))
    return rows


# ----------------------------------------------------------------- RTK panel
def _status_word(status: int) -> str:
    return STATUS_TEXT.get(status, "").strip() or "n/a"


def _base_rows(base: Base) -> list[str]:
    return [
        "  %-14s%s" % ("Status", _status_word(base.status).rjust(12)),
        "  %-14s%s m" % ("East", _or_na(base.east, "% 12.4f", 12)),
        "  %-14s%s m" % ("North", _or_na(base.north, "% 12.4f", 12)),
        "  %-14s%s m" % ("Up", _or_na(base.up, "% 12.4f", 12)),
        "  %-14s%s m" % ("Length", _or_na(base.length, "% 12.4f", 12)),
        "  %-14s%s deg" % ("Course", _or_na(base.course, "% 12.4f", 12)),
    ]


def rtk_att_lines(att: Attitude | None) -> list[str]:
    if att is None:
        return ["  no data yet"]
    rows = [
        "  %-14s%s" % ("Time", iso8601(att.time) if att.time else "n/a"),
        "  %-14s%s deg" % ("Heading", _or_na(att.heading, "% 12.4f", 12)),
        "  %-14s%s deg" % ("Pitch", _or_na(att.pitch, "% 12.4f", 12)),
        "  %-14s%s deg" % ("Roll", _or_na(att.roll, "% 12.4f", 12)),
    ]
    return rows + _base_rows(att.base)


def rtk_tpv_lines(fix: Fix) -> list[str]:
    rows = [
        "  %-14s%s" % ("Time", iso8601(fix.time) if fix.time else "n/a"),
    ]
    rows += _base_rows(fix.base)
    rows += [
        "  %-14s%s s" % ("Age", _or_na(fix.dgps_age, "% 12.4f", 12)),
        "  %-14s%s" % ("Station", str(fix.dgps_station).rjust(12) if fix.dgps_station >= 0 else " " * 12),
        "  %-14s%s" % ("Ratio", _or_na(fix.base.ratio, "% 12.4f", 12)),
    ]
    return rows


# ----------------------------------------------------------------- status bar
def status_lines(st: GpsState, opts: ViewOpts, *, link: str, online: bool,
                 secs: int, rx_bytes: int, raw_on: bool, header: str | None = None,
                 errors: str = "", log_on: bool = False) -> list[str]:
    """The three status rows.  header is the protocol text (default: gpsd's release);
    errors is a counter such as 'bad checksums 2', or '' when there is none."""
    u = opts.units
    if header is None:
        release = st.version.release if st.version else "-"
        header = f"gpsd {release}"
    device = st.device or "-"
    seen, used = st.sats_seen, st.sats_used
    mag = "mag" if opts.magnetic else "true"
    counts = f"   {errors}" if errors else ""
    return [
        f"{link}  device {device}  {header}",
        f"{fix_status(st.fix, online, secs)}   sats {seen}/{used}   rx {rx_bytes} B{counts}",
        f"units {u.alt_units}/{u.speed_units}   lat/lon {opts.deg}   track {mag}   "
        f"raw {'on' if raw_on else 'off'}   log {'on' if log_on else 'off'}",
    ]
