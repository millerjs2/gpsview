"""gpsd JSON messages -> plain state.  No I/O in this module.

Field names and defaults follow gpsd's libgps/libgps_json.c (see man/gpsd_json.adoc):
a missing numeric field is NaN, a missing integer is the default, as in libgps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

NAN = math.nan

# gps.h
MODE_NOT_SEEN, MODE_NO_FIX, MODE_2D, MODE_3D = 0, 1, 2, 3
STATUS_UNK, STATUS_GPS, STATUS_DGPS, STATUS_RTK_FIX, STATUS_RTK_FLT = 0, 1, 2, 3, 4
STATUS_DR, STATUS_GNSSDR, STATUS_TIME, STATUS_SIM, STATUS_PPS_FIX = 5, 6, 7, 8, 9
SAT_HEALTH_UNK, SAT_HEALTH_OK, SAT_HEALTH_BAD = 0, 1, 2

# gpsd API version this client was written against (gps.h GPSD_API_MAJOR/MINOR_VERSION)
API_MAJOR, API_MINOR = 16, 1

# gps.h gnssid_t -> the two-letter codes cgps prints
GNSS_CODES = {0: "GP", 1: "SB", 2: "GA", 3: "BD", 4: "IM", 5: "QZ", 6: "GL", 7: "IR"}


def _num(obj: dict, key: str) -> float:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return NAN
    return float(v)


def _int(obj: dict, key: str, default: int = 0) -> int:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        return default
    return v


def _opt_int(obj: dict, key: str) -> int | None:
    """An integer, or None when the message does not carry it (NMEA and SBP have no leap seconds)."""
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _bool(obj: dict, key: str) -> bool:
    return obj.get(key) is True


def parse_time(value) -> datetime | None:
    """ISO 8601 UTC time from gpsd ("2005-06-08T10:34:48.283Z") or None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class Base:
    """RTK relative position to the base station (TPV baseX / ATT baseX keys)."""
    status: int = STATUS_UNK
    east: float = NAN
    north: float = NAN
    up: float = NAN
    length: float = NAN
    course: float = NAN
    ratio: float = NAN


@dataclass(frozen=True)
class Ecef:
    x: float = NAN
    y: float = NAN
    z: float = NAN
    vx: float = NAN
    vy: float = NAN
    vz: float = NAN


@dataclass(frozen=True)
class Fix:
    """The TPV report."""
    mode: int = MODE_NOT_SEEN
    status: int = STATUS_UNK
    time: datetime | None = None
    latitude: float = NAN
    longitude: float = NAN
    alt_hae: float = NAN
    alt_msl: float = NAN
    speed: float = NAN
    track: float = NAN
    magnetic_track: float = NAN
    magnetic_var: float = NAN
    climb: float = NAN
    epx: float = NAN
    epy: float = NAN
    epv: float = NAN
    eph: float = NAN
    sep: float = NAN
    epd: float = NAN
    eps: float = NAN
    dgps_age: float = NAN
    dgps_station: int = -1
    base: Base = Base()
    ecef: Ecef = Ecef()


@dataclass(frozen=True)
class Dop:
    """The DOP values from SKY."""
    xdop: float = NAN
    ydop: float = NAN
    vdop: float = NAN
    hdop: float = NAN
    pdop: float = NAN
    tdop: float = NAN
    gdop: float = NAN


@dataclass(frozen=True)
class Sat:
    """One satellite in the SKY 'satellites' array."""
    gnssid: int = 0
    svid: int = 0
    sigid: int = 0
    prn: int = 0
    elevation: float = NAN
    azimuth: float = NAN
    ss: float = NAN
    used: bool = False
    health: int = SAT_HEALTH_UNK
    signal: str = ""      # SBP only: the signal, e.g. "L1CA" or "E1B"; "" if not reported


@dataclass(frozen=True)
class Attitude:
    """ATT (heading, pitch, roll, RTK base) or IMU (raw sensor values)."""
    msg: str = ""
    time: datetime | None = None
    time_tag: int = 0
    acc_x: float = NAN
    acc_y: float = NAN
    acc_z: float = NAN
    gyro_temp: float = NAN
    gyro_x: float = NAN
    gyro_y: float = NAN
    gyro_z: float = NAN
    mag_x: float = NAN
    mag_y: float = NAN
    mag_z: float = NAN
    heading: float = NAN
    yaw: float = NAN
    pitch: float = NAN
    roll: float = NAN
    base: Base = Base()


@dataclass(frozen=True)
class Version:
    release: str
    proto_major: int
    proto_minor: int


@dataclass
class GpsState:
    """Everything the views show. apply() updates it from one gpsd message."""
    device: str = ""
    version: Version | None = None
    fix: Fix = field(default_factory=Fix)
    dop: Dop = field(default_factory=Dop)
    sats: tuple[Sat, ...] = ()
    attitude: Attitude | None = None
    imu: Attitude | None = None
    leap_seconds: int | None = None      # None until a message reports it

    # A satellite is counted once, however many signals of it are listed (SBP lists one row
    # per tracked signal).  A satellite is identified by constellation and PRN.
    @property
    def sats_seen(self) -> int:
        return len({(s.gnssid, s.prn) for s in self.sats if s.prn > 0})

    @property
    def sats_used(self) -> int:
        return len({(s.gnssid, s.prn) for s in self.sats if s.used})

    def apply(self, obj: dict) -> str | None:
        """Update from one decoded JSON object. Returns its class, or None if not handled."""
        cls = obj.get("class")
        if not isinstance(cls, str):
            return None
        if isinstance(obj.get("device"), str):
            self.device = obj["device"]
        if cls == "TPV":
            self._tpv(obj)
        elif cls == "SKY":
            self._sky(obj)
        elif cls == "ATT":
            self.attitude = _attitude(obj, with_base=True)
        elif cls == "IMU":
            self.imu = _attitude(obj, with_base=False)
        elif cls == "VERSION":
            self.version = Version(
                release=str(obj.get("release", "")),
                proto_major=_int(obj, "proto_major", -1),
                proto_minor=_int(obj, "proto_minor", -1),
            )
        else:
            return None
        return cls

    def _tpv(self, obj: dict) -> None:
        self.leap_seconds = _opt_int(obj, "leapseconds")
        base = Base(
            status=_int(obj, "baseS", STATUS_UNK),
            east=_num(obj, "baseE"),
            north=_num(obj, "baseN"),
            up=_num(obj, "baseU"),
            length=_num(obj, "baseL"),
            course=_num(obj, "baseC"),
            ratio=_num(obj, "dgpsRatio"),
        )
        ecef = Ecef(
            x=_num(obj, "ecefx"), y=_num(obj, "ecefy"), z=_num(obj, "ecefz"),
            vx=_num(obj, "ecefvx"), vy=_num(obj, "ecefvy"), vz=_num(obj, "ecefvz"),
        )
        self.fix = Fix(
            mode=_int(obj, "mode", MODE_NOT_SEEN),
            status=_int(obj, "status", STATUS_UNK),
            time=parse_time(obj.get("time")),
            latitude=_num(obj, "lat"),
            longitude=_num(obj, "lon"),
            alt_hae=_num(obj, "altHAE"),
            alt_msl=_num(obj, "altMSL"),
            speed=_num(obj, "speed"),
            track=_num(obj, "track"),
            magnetic_track=_num(obj, "magtrack"),
            magnetic_var=_num(obj, "magvar"),
            climb=_num(obj, "climb"),
            epx=_num(obj, "epx"),
            epy=_num(obj, "epy"),
            epv=_num(obj, "epv"),
            eph=_num(obj, "eph"),
            sep=_num(obj, "sep"),
            epd=_num(obj, "epd"),
            eps=_num(obj, "eps"),
            dgps_age=_num(obj, "dgpsAge"),
            dgps_station=_int(obj, "dgpsSta", -1),
            base=base,
            ecef=ecef,
        )

    def _sky(self, obj: dict) -> None:
        self.dop = Dop(
            xdop=_num(obj, "xdop"), ydop=_num(obj, "ydop"), vdop=_num(obj, "vdop"),
            hdop=_num(obj, "hdop"), pdop=_num(obj, "pdop"), tdop=_num(obj, "tdop"),
            gdop=_num(obj, "gdop"),
        )
        # A SKY without a satellites array (DOP only) keeps the last sky view.
        sats = obj.get("satellites")
        if isinstance(sats, list):
            self.sats = tuple(_sat(s) for s in sats if isinstance(s, dict))


def _sat(obj: dict) -> Sat:
    return Sat(
        gnssid=_int(obj, "gnssid", 0),
        svid=_int(obj, "svid", 0),
        sigid=_int(obj, "sigid", 0),
        prn=_int(obj, "PRN", 0),
        elevation=_num(obj, "el"),
        azimuth=_num(obj, "az"),
        ss=_num(obj, "ss"),
        used=_bool(obj, "used"),
        health=_int(obj, "health", SAT_HEALTH_UNK),
        signal=obj.get("signal") if isinstance(obj.get("signal"), str) else "",
    )


def _attitude(obj: dict, with_base: bool) -> Attitude:
    base = Base()
    if with_base:
        base = Base(
            status=_int(obj, "baseS", STATUS_UNK),
            east=_num(obj, "baseE"),
            north=_num(obj, "baseN"),
            up=_num(obj, "baseU"),
            length=_num(obj, "baseL"),
            course=_num(obj, "baseC"),
            ratio=_num(obj, "dgpsRatio"),
        )
    return Attitude(
        msg=str(obj.get("msg", "")),
        time=parse_time(obj.get("time")),
        time_tag=_int(obj, "timeTag", 0),
        acc_x=_num(obj, "acc_x"),
        acc_y=_num(obj, "acc_y"),
        acc_z=_num(obj, "acc_z"),
        gyro_temp=_num(obj, "gyro_temp"),
        gyro_x=_num(obj, "gyro_x"),
        gyro_y=_num(obj, "gyro_y"),
        gyro_z=_num(obj, "gyro_z"),
        mag_x=_num(obj, "mag_x"),
        mag_y=_num(obj, "mag_y"),
        mag_z=_num(obj, "mag_z"),
        heading=_num(obj, "heading"),
        yaw=_num(obj, "yaw"),
        pitch=_num(obj, "pitch"),
        roll=_num(obj, "roll"),
        base=base,
    )
