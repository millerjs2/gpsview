"""Swift Binary Protocol (SBP) decoder: frames from a Swift receiver, turned into gpsd-shaped
TPV, SKY, ATT objects.

Framing (libsbp spec): 0x55 preamble, u16 message type, u16 sender, u8 payload length,
payload, then a CRC-16 (CRC-16/XMODEM) of everything after the preamble, little-endian.
Payloads are decoded by the `sbp` package (pip install sbp).

Units and enums are from libsbp's spec/yaml (checked against the files, not guessed):
    POS_LLH      lat/lon in degrees, height in m, accuracies in mm, flags bits 0-2 = fix mode
    VEL_NED      mm/s
    BASELINE_NED mm, flags bits 0-2 = mode (same enum as POS_LLH)
    DOPS         0.01 units
    SV_AZ_EL     az in units of 2 degrees, el in degrees
    MEASUREMENT_STATE cn0 in units of dB-Hz / 4; zero means invalid

Satellites: a satellite is listed when SV_AZ_EL or MEASUREMENT_STATE reports it.  "Used" is
true when the channel is tracking (cn0 > 0).  The stream has no per-satellite "used in the
solution" flag, so that is the closest equivalent; the table says so in the README.
"""
from __future__ import annotations

import binascii
import math
import warnings
from datetime import datetime, timezone

from sbp.msg import SBP
from sbp.table import dispatch

from .feeds import Event, Feed, obj, raw
from .model import MODE_NO_FIX, MODE_3D
from .transports import Endpoint
from .views import iso8601

PREAMBLE = 0x55
HEADER = 6                                   # preamble, type, sender, length
CRC_LEN = 2

# libsbp spec, gnss.yaml: GnssSignal.code -> (gpsd gnssid, signal name).  gnssid is the
# gps.h number.  The signal name is libsbp's name without the constellation prefix.
SIGNALS = {
    0: (0, "L1CA"), 1: (0, "L2CM"), 5: (0, "L1P"), 6: (0, "L2P"),       # GPS
    2: (1, "L1CA"),                                                    # SBAS
    3: (6, "L1CA"), 4: (6, "L2CA"),                                    # GLONASS
    12: (3, "B1"), 13: (3, "B2"), 47: (3, "B2a"),                      # BeiDou
    14: (2, "E1B"), 20: (2, "E7I"),                                    # Galileo
    31: (5, "L1CA"), 36: (5, "L2CL"), 39: (5, "L5Q"),                  # QZSS
}
# navigation.yaml fix mode (flags bits 0-2): gpsd mode and status
FIX_MODE = {1: MODE_3D, 2: MODE_3D, 3: MODE_3D, 4: MODE_3D, 6: MODE_3D}
FIX_STATUS = {1: 1, 2: 2, 3: 4, 4: 3, 6: 2}  # SPP->GPS, DGNSS->DGPS, float->RTK_FLT,
                                             # fixed->RTK_FIX, SBAS->DGPS (gps.h STATUS_*)
GLO_SLOTS = range(1, 29)
# gpsd's PRN for a satellite number, as its NMEA driver numbers them (driver_nmea0183.c):
# GLONASS 64 + slot, Galileo 300 + svid, BeiDou 400 + svid.  The same satellite then has the
# same PRN from NMEA and from SBP.  Other constellations use the number as it is.
PRN_OFFSET = {6: 64, 2: 300, 3: 400}


class SbpDecoder:
    protocol = "SBP"
    error_label = "bad CRC"

    def __init__(self, device: str) -> None:
        self._device = device
        self._buf = bytearray()
        self.errors = 0
        self._time: str | None = None
        self._pos: dict = {"mode": MODE_NO_FIX}
        self._vel: dict = {}
        self._dop: dict = {}
        self._base: dict = {}
        self._azel: dict[tuple[int, int], tuple[float, float]] = {}   # (code, sat) -> (az, el)
        self._cn0: dict[tuple[int, int], float] = {}                  # (code, sat) -> dB-Hz

    def on_open(self) -> bytes:
        return b""                            # read-only: nothing is sent to the receiver

    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        while True:
            start = self._buf.find(PREAMBLE)
            if start < 0:
                self._buf.clear()
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < HEADER:
                break
            length = self._buf[5]
            total = HEADER + length + CRC_LEN
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            crc = int.from_bytes(frame[HEADER + length:total], "little")
            if binascii.crc_hqx(frame[1:HEADER + length], 0) != crc:
                self.errors += 1
                del self._buf[:1]             # resynchronise on the next preamble
                continue
            del self._buf[:total]
            events.extend(self._frame(frame))
        return events

    # one frame ------------------------------------------------------------
    def _frame(self, frame: bytes) -> list[Event]:
        msg_type = frame[1] | frame[2] << 8
        sender = frame[3] | frame[4] << 8
        length = frame[5]
        typed = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # sbp warns for message types it does not know
            try:
                typed = dispatch(SBP.unpack(frame))
            except Exception:
                typed = None
        name = type(typed).__name__ if typed is not None else "SBP"
        if name == "SBP":                     # not in the table: show it, decode nothing
            return [raw(f"SBP unknown 0x{msg_type:04X} sender {sender} {length} bytes")]
        events = [raw(f"SBP {name.removeprefix('Msg')} 0x{msg_type:04X} "
                      f"sender {sender} {length} bytes")]
        handler = {"MsgPosLLH": self._pos_llh, "MsgUtcTime": self._utc,
                   "MsgVelNED": self._vel_ned, "MsgDops": self._dops,
                   "MsgSvAzEl": self._azel_msg,
                   "MsgMeasurementState": self._measurement,
                   "MsgBaselineNED": self._baseline}.get(name)
        if handler is not None:
            events.extend(handler(typed))
        return events

    # messages -------------------------------------------------------------
    def _pos_llh(self, m) -> list[Event]:
        bits = m.flags & 0x7
        if bits not in FIX_MODE:
            self._pos = {"mode": MODE_NO_FIX}
        else:
            h_err, v_err = m.h_accuracy / 1000.0, m.v_accuracy / 1000.0
            self._pos = {
                "mode": FIX_MODE[bits], "status": FIX_STATUS.get(bits, 1),
                "lat": m.lat, "lon": m.lon, "altHAE": m.height,
                "epx": h_err, "epy": h_err, "epv": v_err,
                "eph": h_err, "sep": math.hypot(h_err, v_err),
            }
        return [self._tpv_event()]

    def _utc(self, m) -> list[Event]:
        if m.flags & 0x7:                     # time source valid
            dt = datetime(m.year, m.month, m.day, m.hours, m.minutes, m.seconds,
                          m.ns // 1000, tzinfo=timezone.utc)
            self._time = iso8601(dt)
        return []

    def _vel_ned(self, m) -> list[Event]:
        if m.flags & 0x7:
            self._vel = {"speed": math.hypot(m.n, m.e) / 1000.0,
                         "track": math.degrees(math.atan2(m.e, m.n)) % 360.0,
                         "climb": -m.d / 1000.0}
        else:
            self._vel = {}
        return []                                # merged into the next TPV

    def _dops(self, m) -> list[Event]:
        if m.flags & 0x7:
            self._dop = {"gdop": m.gdop * 0.01, "pdop": m.pdop * 0.01, "tdop": m.tdop * 0.01,
                         "hdop": m.hdop * 0.01, "vdop": m.vdop * 0.01}
        else:
            self._dop = {}
        return [self._sky_event()]

    def _azel_msg(self, m) -> list[Event]:
        self._azel = {(a.sid.code, a.sid.sat): (a.az * 2.0, float(a.el)) for a in m.azel}
        return [self._sky_event()]

    def _measurement(self, m) -> list[Event]:
        self._cn0 = {(s.mesid.code, s.mesid.sat): s.cn0 / 4.0 for s in m.states if s.cn0}
        return [self._sky_event()]

    def _baseline(self, m) -> list[Event]:
        # RTK: the rover relative to the base station, in NED.  Shown as the base in TPV/ATT.
        bits = m.flags & 0x7
        if bits not in FIX_MODE:
            self._base = {}
        else:
            n, e, d = m.n / 1000.0, m.e / 1000.0, m.d / 1000.0
            self._base = {
                "baseS": FIX_STATUS.get(bits, 1), "baseN": n, "baseE": e, "baseU": -d,
                "baseL": math.sqrt(n * n + e * e + d * d),
                "baseC": math.degrees(math.atan2(e, n)) % 360.0,
            }
        return [self._tpv_event(), obj({"class": "ATT", "device": self._device,
                                        **({"time": self._time} if self._time else {}),
                                        **self._base})]

    # gpsd-shaped output ----------------------------------------------------
    def _tpv_event(self) -> Event:
        value: dict = {"class": "TPV", "device": self._device, **self._pos}
        if self._time:
            value["time"] = self._time
        value.update(self._vel)
        value.update(self._base)
        return obj(value)

    def _sky_event(self) -> Event:
        # Elevation and azimuth are given once per satellite (SV_AZ_EL), while the
        # measurements come once per tracked signal.  A signal with no geometry of its own
        # takes its satellite's, found by constellation and number.
        geometry: dict[tuple[int, int], tuple[float, float]] = {}
        for (code, sat), angles in self._azel.items():
            if code in SIGNALS:
                geometry.setdefault((SIGNALS[code][0], sat), angles)
        sats: list[dict] = []
        for code, sat in sorted(set(self._azel) | set(self._cn0)):
            if code not in SIGNALS:
                continue
            gnssid, signal = SIGNALS[code]
            if gnssid == 6 and sat not in GLO_SLOTS:
                continue                      # GLONASS slot 1..28 only
            prn = sat + PRN_OFFSET.get(gnssid, 0)
            entry: dict = {"PRN": prn, "gnssid": gnssid, "svid": sat, "sigid": 0,
                           "signal": signal, "used": (code, sat) in self._cn0}
            angles = self._azel.get((code, sat)) or geometry.get((gnssid, sat))
            if angles is not None:
                az, el = angles
                entry.update(az=az, el=el)
            if (code, sat) in self._cn0:
                entry["ss"] = self._cn0[(code, sat)]
            sats.append(entry)
        value: dict = {"class": "SKY", "device": self._device, "satellites": sats}
        value.update(self._dop)
        return obj(value)


def sbp_feed(endpoint: Endpoint) -> Feed:
    return Feed(
        title="SBP",
        label=endpoint.label,
        header="SBP",
        make_transport=endpoint.make_transport,
        endpoint=endpoint,
        protocol="sbp",
        make_decoder=lambda: SbpDecoder(endpoint.device),
    )
