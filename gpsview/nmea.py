"""NMEA 0183 decoder: $-sentences from a receiver, turned into gpsd-shaped TPV and SKY objects.

Sentences handled: GGA (position, time, altitude, quality), RMC (position, speed, track,
date, magnetic variation), VTG (track and speed), GSA (fix mode, used satellites, DOPs),
GSV (satellites in view), GST (error estimates), ZDA (date).

A TPV is emitted after each GGA, RMC or GST, and a SKY after each GSA or complete GSV set.  The
fields come from the latest sentences, as gpsd would report them for one epoch.

Not handled: heading (HDT), GBS, proprietary sentences.  Checksum failures are counted in
the status bar and their sentence is not used.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from .feeds import Event, Feed, obj, raw
from .model import MODE_2D, MODE_3D, MODE_NO_FIX
from .transports import Endpoint
from .views import iso8601

KNOT_MPS = 0.514444             # metres per second in a knot
KMH_MPS = 1 / 3.6
GGA_STATUS = {0: 0, 1: 1, 2: 2, 3: 9, 4: 3, 5: 4, 6: 5}   # GGA quality -> gps.h STATUS_*
# NMEA talker -> gpsd gnssid (gps.h: 0 GPS, 1 SBAS, 2 Galileo, 3 BeiDou, 5 QZSS, 6 GLONASS, 7 NavIC)
TALKER_GNSSID = {"GP": 0, "GN": 0, "SB": 1, "GA": 2, "BD": 3, "GB": 3, "QZ": 5, "GQ": 5,
                 "GL": 6, "GI": 7}


def checksum(body: str) -> int:
    """XOR of the characters between '$' and '*'."""
    value = 0
    for ch in body:
        value ^= ord(ch)
    return value


def make_sentence(body: str) -> bytes:
    """A sentence with its checksum, as a receiver would send it (used by the fake feeder)."""
    return f"${body}*{checksum(body):02X}\r\n".encode("ascii")


# gpsd status -> GGA quality indicator (the reverse of GGA_STATUS)
QUALITY_FOR_STATUS = {1: 1, 2: 2, 3: 4, 4: 5, 5: 6, 6: 6, 7: 7, 8: 8, 9: 3}


def gga_sentence(fix, sats_used: int, hdop: float, now: datetime) -> bytes | None:
    """A GGA sentence for the current fix, as an NTRIP caster wants it (VRS networks pick the
    nearby base stations from it).  None when there is no position yet.

    `fix` is a model.Fix.  The altitude is above mean sea level where the receiver gives it,
    and height above the ellipsoid otherwise (a few tens of metres off: harmless for this).
    """
    if fix.mode < MODE_2D or math.isnan(fix.latitude) or math.isnan(fix.longitude):
        return None
    stamp = fix.time or now
    alt = fix.alt_msl if not math.isnan(fix.alt_msl) else fix.alt_hae
    lat, lon = abs(fix.latitude), abs(fix.longitude)
    lat_d, lon_d = int(lat), int(lon)
    body = ",".join([
        "GPGGA", f"{stamp:%H%M%S}.{stamp.microsecond // 10000:02d}",
        f"{lat_d:02d}{(lat - lat_d) * 60:010.7f}", "N" if fix.latitude >= 0 else "S",
        f"{lon_d:03d}{(lon - lon_d) * 60:010.7f}", "E" if fix.longitude >= 0 else "W",
        str(QUALITY_FOR_STATUS.get(fix.status, 1)), f"{min(sats_used, 99):02d}",
        "" if math.isnan(hdop) else f"{hdop:.1f}",
        "" if math.isnan(alt) else f"{alt:.3f}", "M", "", "M", "", ""])
    return make_sentence(body)


def split_sentence(text: str) -> tuple[list[str], bool] | None:
    """Fields of one sentence, and whether its checksum is good.  None if it is not a sentence.

    A sentence with no '*' has no checksum to test and is accepted.
    """
    if not text.startswith(("$", "!")):
        return None
    body, star, given = text[1:].partition("*")
    if not star:
        return body.split(","), True
    try:
        good = int(given.strip()[:2], 16) == checksum(body)
    except ValueError:
        good = False
    return body.split(","), good


def _f(fields: list[str], i: int) -> str:
    return fields[i] if i < len(fields) else ""


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _coord(value: str, hemi: str, deg_digits: int) -> float | None:
    """ddmm.mmmm (or dddmm.mmmm) with N/S or E/W, as signed decimal degrees."""
    if len(value) <= deg_digits or hemi not in ("N", "S", "E", "W"):
        return None
    deg, minutes = _int(value[:deg_digits]), _float(value[deg_digits:])
    if deg is None or minutes is None:
        return None
    result = deg + minutes / 60.0
    return -result if hemi in ("S", "W") else result


def _hms(text: str) -> tuple[int, int, float] | None:
    if len(text) < 6:
        return None
    h, m, s = _int(text[0:2]), _int(text[2:4]), _float(text[4:])
    if h is None or m is None or s is None:
        return None
    return h, m, s


class NmeaDecoder:
    protocol = "NMEA"
    error_label = "bad checksums"

    def __init__(self, device: str) -> None:
        self._device = device
        self._buf = b""
        self.errors = 0
        self._date: date | None = None
        self._tpv: dict = {}              # gpsd TPV keys, latest value of each
        self._dop: dict = {}              # gpsd SKY DOP keys
        self._quality: int | None = None  # GGA fix quality
        self._gsa_mode: int | None = None # GSA fix type: 1 none, 2 2D, 3 3D
        self._rmc_void = False            # RMC status V
        self._used: set[int] = set()      # PRNs in this epoch's GSA sentences (one per system)
        self._sats: dict[tuple[int, int], dict] = {}     # (gnssid, PRN) -> satellite
        self._gsv_pending: dict[int, list[dict]] = {}    # satellites of a GSV set in progress

    # gpsd-style interface -------------------------------------------------
    def on_open(self) -> bytes:
        return b""                        # read-only: nothing is sent to the receiver

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            text = line.decode("ascii", "replace").strip()
            if not text:
                continue
            parsed = split_sentence(text)
            if parsed is None:
                events.append(raw(text))
                continue
            fields, good = parsed
            if not good:
                self.errors += 1
                events.append(raw(text, "red"))
                continue
            events.append(raw(text))
            try:
                events.extend(self._sentence(fields))
            except (IndexError, ValueError, OverflowError):
                self.errors += 1          # sentence with a malformed field
        return events

    # sentences ------------------------------------------------------------
    def _sentence(self, fields: list[str]) -> list[Event]:
        kind = fields[0][-3:]
        handler = {"GGA": self._gga, "RMC": self._rmc, "VTG": self._vtg, "GSA": self._gsa,
                   "GSV": self._gsv, "GST": self._gst, "ZDA": self._zda}.get(kind)
        return handler(fields) if handler else []

    def _gga(self, f: list[str]) -> list[Event]:
        # 1 time  2 lat  3 N/S  4 lon  5 E/W  6 quality  7 satellites  8 hdop
        # 9 altitude (MSL)  10 M  11 geoid separation  12 M
        self._used = set()                 # a new epoch: its GSA sentences rebuild the used set
        self._clock(_f(f, 1))
        self._quality = _int(_f(f, 6)) or 0
        lat = _coord(_f(f, 2), _f(f, 3), 2)
        lon = _coord(_f(f, 4), _f(f, 5), 3)
        alt = _float(_f(f, 9))
        sep = _float(_f(f, 11))
        self._set("lat", lat)
        self._set("lon", lon)
        self._set("altMSL", alt)
        self._set("altHAE", alt + sep if alt is not None and sep is not None else None)
        self._set("status", GGA_STATUS.get(self._quality, 0))
        return [self._tpv_event()]

    def _rmc(self, f: list[str]) -> list[Event]:
        # 1 time  2 status A/V  3 lat  4 N/S  5 lon  6 E/W  7 speed kn  8 track  9 date
        # 10 magnetic variation  11 E/W  12 mode
        self._rmc_void = _f(f, 2) != "A"
        if len(_f(f, 9)) == 6:
            d, m, y = _int(f[9][0:2]), _int(f[9][2:4]), _int(f[9][4:6])
            if d is not None and m is not None and y is not None:
                self._date = date(1900 + y if y >= 70 else 2000 + y, m, d)   # 2-digit year
        self._clock(_f(f, 1))
        if not self._rmc_void:
            self._set("lat", _coord(_f(f, 3), _f(f, 4), 2))
            self._set("lon", _coord(_f(f, 5), _f(f, 6), 3))
        speed = _float(_f(f, 7))
        self._set("speed", speed * KNOT_MPS if speed is not None else None)
        self._set("track", _float(_f(f, 8)))
        var = _float(_f(f, 10))
        if var is not None:
            var = -var if _f(f, 11) == "W" else var
        self._set("magvar", var)
        return [self._tpv_event()]

    def _vtg(self, f: list[str]) -> list[Event]:
        # 1 track true  3 track magnetic  5 speed kn  7 speed km/h
        self._set("track", _float(_f(f, 1)))
        self._set("magtrack", _float(_f(f, 3)))
        kmh = _float(_f(f, 7))
        self._set("speed", kmh * KMH_MPS if kmh is not None else None)
        return []

    def _gsa(self, f: list[str]) -> list[Event]:
        # 1 A/M  2 fix type 1/2/3  3..14 PRNs in the solution  15 pdop  16 hdop  17 vdop
        self._gsa_mode = _int(_f(f, 2))
        # One GSA per constellation (GNGSA in a multi-GNSS epoch): the used set is the union.
        self._used |= {p for p in (_int(x) for x in f[3:15]) if p}
        self._dop = {"pdop": _float(_f(f, 15)), "hdop": _float(_f(f, 16)),
                     "vdop": _float(_f(f, 17))}
        return [self._sky_event()]

    def _gsv(self, f: list[str]) -> list[Event]:
        # 1 messages  2 this message number  3 satellites in view, then groups of 4:
        # PRN, elevation, azimuth, SNR (the last group may be followed by a signal id)
        gnssid = TALKER_GNSSID.get(f[0][:2], 0)
        total, number = _int(_f(f, 1)), _int(_f(f, 2))
        if total is None or number is None:
            raise ValueError("GSV counts")
        if number == 1:
            self._gsv_pending[gnssid] = []
        pending = self._gsv_pending.setdefault(gnssid, [])
        for i in range(4, len(f) - 3, 4):
            prn = _int(f[i])
            if not prn:
                continue
            sat: dict = {"PRN": prn, "gnssid": gnssid, "svid": prn, "sigid": 0}
            for key, value in (("el", _float(f[i + 1])), ("az", _float(f[i + 2])),
                               ("ss", _float(f[i + 3]))):
                if value is not None:
                    sat[key] = value
            pending.append(sat)
        if number != total:
            return []
        self._sats = {k: v for k, v in self._sats.items() if k[0] != gnssid}
        for sat in pending:
            self._sats[(gnssid, sat["PRN"])] = sat
        self._gsv_pending[gnssid] = []
        return [self._sky_event()]

    def _gst(self, f: list[str]) -> list[Event]:
        # 1 time  2 RMS  3 std major  4 std minor  5 orientation  6 std lat  7 std lon  8 std alt
        lat_sd, lon_sd, alt_sd = _float(_f(f, 6)), _float(_f(f, 7)), _float(_f(f, 8))
        self._set("epy", lat_sd)
        self._set("epx", lon_sd)
        self._set("epv", alt_sd)
        eph = math.hypot(lat_sd, lon_sd) if lat_sd is not None and lon_sd is not None else None
        self._set("eph", eph)
        self._set("sep", math.hypot(eph, alt_sd)
                  if eph is not None and alt_sd is not None else None)
        return [self._tpv_event()]          # GST usually follows GGA/RMC in the epoch

    def _zda(self, f: list[str]) -> list[Event]:
        # 1 time  2 day  3 month  4 year
        d, m, y = _int(_f(f, 2)), _int(_f(f, 3)), _int(_f(f, 4))
        if d is not None and m is not None and y is not None:
            self._date = date(y, m, d)
        self._clock(_f(f, 1))
        return []

    # state ----------------------------------------------------------------
    def _set(self, key: str, value) -> None:
        if value is None:
            self._tpv.pop(key, None)
        else:
            self._tpv[key] = value

    def _clock(self, text: str) -> None:
        hms = _hms(text)
        if hms is None:
            return
        day = self._date or datetime.now(timezone.utc).date()
        h, m, s = hms
        dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        dt += timedelta(hours=h, minutes=m, seconds=s)
        self._set("time", iso8601(dt))

    def _mode(self) -> int:
        if self._rmc_void or self._quality == 0:
            return MODE_NO_FIX
        if self._gsa_mode in (MODE_NO_FIX, MODE_2D, MODE_3D):
            return self._gsa_mode
        if "lat" not in self._tpv:
            return MODE_NO_FIX
        return MODE_3D if "altMSL" in self._tpv else MODE_2D

    def _tpv_event(self) -> Event:
        return obj({"class": "TPV", "device": self._device, "mode": self._mode(), **self._tpv})

    def _sky_event(self) -> Event:
        sats = []
        for (gnssid, prn), sat in self._sats.items():
            sats.append(dict(sat, used=prn in self._used))
        value = {"class": "SKY", "device": self._device, "satellites": sats}
        value.update({k: v for k, v in self._dop.items() if v is not None})
        return obj(value)


def nmea_feed(endpoint: Endpoint) -> Feed:
    return Feed(
        title="NMEA",
        label=endpoint.label,
        header="NMEA 0183",
        make_transport=endpoint.make_transport,
        endpoint=endpoint,
        protocol="nmea",
        make_decoder=lambda: NmeaDecoder(endpoint.device),
    )
