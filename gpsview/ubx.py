"""u-blox UBX decoder: frames from a u-blox receiver, turned into gpsd-shaped TPV, SKY and
ATT objects.

Framing (u-blox interface description): 0xB5 0x62, class, id, u16 little-endian length,
payload, then a two-byte Fletcher checksum (CK_A, CK_B) over class, id, length and payload.

Messages used (layouts checked against gpsd's drivers/driver_ubx.c):
    NAV-PVT        0x01 0x07  fix, time, position, speed, heading, accuracies, RTK state
    NAV-DOP        0x01 0x04  GDOP, PDOP, TDOP, VDOP, HDOP
    NAV-SAT        0x01 0x35  satellites: elevation, azimuth, C/N0, used (version 1)
    NAV-SIG        0x01 0x43  signals: C/N0, used, health, signal name (version 0)
    NAV-RELPOSNED  0x01 0x3C  RTK: this receiver relative to the base (versions 0 and 1)
    RXM-RTCM       0x02 0x32  the receiver reporting RTCM corrections it received (M8P and
                              similar)
    RXM-COR        0x02 0x34  the same report from F9 receivers: protocol, error, used, type
Every other message is listed in the message pane as one line.

u-blox receivers send NMEA by default, and a base station may send RTCM 3, on the same
port as UBX.  The decoder steps over both.  NMEA lines are shown in the message pane but not
used; RTCM frames are shown as a type and length.  Only UBX NAV messages feed the panels,
so a mixed stream does not make the display flicker between two decoders.

High precision (ZED-F9P and other high-precision firmware, and what an RTK fix produces):
NAV-HPPOSLLH replaces the PVT position with one carrying the extra digits (lat/lon to 1e-9
degree, altitude to 0.1 mm) and its accuracy; NAV-HPPOSECEF, or NAV-POSECEF without it, gives
the ECEF position, and NAV-VELECEF the ECEF velocity.  Each applies only to the epoch (iTOW) of
the NAV-PVT it belongs to, so a stale one is never mixed into a newer fix.

Not supported: NAV-SVINFO (u-blox 6 and 7, and older), NAV-ATT, ESF
(the IMU tab stays empty).  NAV-SAT needs protocol version 15 or later (u-blox 8, 9, 10).
"""
from __future__ import annotations

import math
import re
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import rtcm
from .feeds import Event, Feed, note, obj, raw
from .model import MODE_2D, MODE_3D, MODE_NO_FIX
from .transports import Endpoint
from .views import iso8601

SYNC1, SYNC2 = 0xB5, 0x62
NMEA_LINE = re.compile(rb"\$[\x20-\x7e]*\r?\n\Z")
HEADER = 6                        # sync (2), class, id, length (2)
CK_LEN = 2
MAX_PAYLOAD = 8192                # larger than any message used; stops a false sync from stalling
NMEA_MAX = 160                    # longest NMEA line (82 standard; proprietary ones are longer)
NO_NAV_AFTER = 5.0                # seconds of NMEA with no NAV-PVT before we say what to enable

# UBX classes that exist; a frame whose class is not here is not a UBX frame.
CLASSES = {0x01: "NAV", 0x02: "RXM", 0x04: "INF", 0x05: "ACK", 0x06: "CFG", 0x09: "UPD",
           0x0A: "MON", 0x0B: "AID", 0x0D: "TIM", 0x10: "ESF", 0x13: "MGA", 0x21: "LOG",
           0x27: "SEC", 0x28: "HNR", 0x29: "NAV2"}
# (class, id) -> name, for the message pane.  Others show as their number.
NAMES = {
    (0x01, 0x01): "NAV-POSECEF", (0x01, 0x02): "NAV-POSLLH", (0x01, 0x03): "NAV-STATUS",
    (0x01, 0x04): "NAV-DOP", (0x01, 0x07): "NAV-PVT", (0x01, 0x11): "NAV-VELECEF",
    (0x01, 0x12): "NAV-VELNED", (0x01, 0x13): "NAV-HPPOSECEF", (0x01, 0x14): "NAV-HPPOSLLH",
    (0x01, 0x17): "NAV-PVAT", (0x01, 0x20): "NAV-TIMEGPS", (0x01, 0x21): "NAV-TIMEUTC",
    (0x01, 0x22): "NAV-CLOCK", (0x01, 0x26): "NAV-TIMELS", (0x01, 0x30): "NAV-SVINFO",
    (0x01, 0x35): "NAV-SAT", (0x01, 0x3C): "NAV-RELPOSNED", (0x01, 0x3D): "NAV-EELL",
    (0x01, 0x43): "NAV-SIG", (0x01, 0x61): "NAV-EOE", (0x02, 0x13): "RXM-SFRBX",
    (0x02, 0x15): "RXM-RAWX", (0x02, 0x32): "RXM-RTCM", (0x02, 0x34): "RXM-COR", (0x05, 0x00): "ACK-NAK",
    (0x05, 0x01): "ACK-ACK", (0x0A, 0x04): "MON-VER", (0x0A, 0x09): "MON-HW",
    (0x0A, 0x28): "MON-HW3", (0x10, 0x15): "ESF-INS", (0x10, 0x02): "ESF-MEAS",
}

# gps.h STATUS_*
STATUS_GPS, STATUS_DGPS, STATUS_RTK_FIX, STATUS_RTK_FLT = 1, 2, 3, 4
STATUS_DR, STATUS_GNSSDR, STATUS_TIME = 5, 6, 7

# NAV-PVT fixType -> (gpsd mode, gpsd status)
FIX_TYPE = {1: (MODE_2D, STATUS_DR), 2: (MODE_2D, STATUS_GPS), 3: (MODE_3D, STATUS_GPS),
            4: (MODE_3D, STATUS_GNSSDR), 5: (MODE_3D, STATUS_TIME)}

# (gnssId, sigId) -> signal name, from gpsd's vgnss_sig_ids and the u-blox interface
# description.  The constellation is the GNSS column, so it is left out of the name.
SIGNALS = {
    (0, 0): "L1CA", (0, 3): "L2CL", (0, 4): "L2CM", (0, 6): "L5I", (0, 7): "L5Q",
    (1, 0): "L1CA",
    (2, 0): "E1C", (2, 1): "E1B", (2, 3): "E5aI", (2, 4): "E5aQ", (2, 5): "E5bI",
    (2, 6): "E5bQ", (2, 8): "E6B", (2, 9): "E6C", (2, 10): "E6A",
    (3, 0): "B1ID1", (3, 1): "B1ID2", (3, 2): "B2ID1", (3, 3): "B2ID2", (3, 4): "B3ID1",
    (3, 5): "B1Cp", (3, 6): "B1Cd", (3, 7): "B2ap", (3, 8): "B2ad", (3, 10): "B3ID2",
    (5, 0): "L1CA", (5, 1): "L1S", (5, 4): "L2CM", (5, 5): "L2CL", (5, 8): "L5I",
    (5, 9): "L5Q", (5, 12): "L1CB",
    (6, 0): "L1OF", (6, 2): "L2OF",
    (7, 0): "L5A",
}


def ubx_prn(gnss_id: int, sv_id: int) -> int:
    """gpsd's PRN for a UBX (gnssId, svId), as libgps/gpsutils.c ubx2_to_prn().  0 = skip."""
    if sv_id < 1:
        return 0
    if gnss_id == 0:
        return sv_id if sv_id <= 32 else 0
    if gnss_id == 1:                                  # SBAS
        if 120 <= sv_id <= 151:
            return sv_id - 87
        return sv_id if 152 <= sv_id <= 158 else 0
    if gnss_id == 2:                                  # Galileo
        if sv_id <= 36:
            return sv_id + 300
        return sv_id + 90 if 211 <= sv_id <= 246 else 0
    if gnss_id == 3:                                  # BeiDou
        if sv_id <= 63:
            return sv_id + 400
        return sv_id + 242 if 159 <= sv_id <= 163 else 0
    if gnss_id == 4:                                  # IMES
        return sv_id + 172 if sv_id <= 10 else 0
    if gnss_id == 5:                                  # QZSS
        if sv_id <= 10:
            return sv_id + 192
        return sv_id if 193 <= sv_id <= 202 else 0
    if gnss_id == 6:                                  # GLONASS (255 = tracked, not identified)
        if sv_id <= 32:
            return sv_id + 64
        return sv_id if 65 <= sv_id <= 96 else 0
    if gnss_id == 7:                                  # NavIC
        return sv_id + 800 if sv_id <= 14 else 0
    return 0


def fletcher(data: bytes | bytearray) -> tuple[int, int]:
    a = b = 0
    for x in data:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return a, b


def make_frame(msg_class: int, msg_id: int, payload: bytes) -> bytes:
    """A complete frame (used by tests and the fake receiver)."""
    body = bytes([msg_class, msg_id]) + struct.pack("<H", len(payload)) + payload
    return bytes([SYNC1, SYNC2]) + body + bytes(fletcher(body))


def _status(flags: int) -> int:
    """gpsd status from NAV-PVT flags: bit 1 diffSoln, bits 6-7 carrSoln (1 float, 2 fixed)."""
    carr = (flags >> 6) & 3
    if carr == 2:
        return STATUS_RTK_FIX
    if carr == 1:
        return STATUS_RTK_FLT
    return STATUS_DGPS if flags & 0x02 else STATUS_GPS


class UbxDecoder:
    protocol = "UBX"
    error_label = "bad checksum"

    def __init__(self, device: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._device = device
        self._clock = clock
        self._buf = bytearray()
        self.errors = 0
        self.rtcm_ok = 0              # RXM-RTCM: corrections the receiver accepted
        self.rtcm_failed = 0          # ... and the ones that failed their CRC
        self.rtcm_last: tuple[int, bool, int] | None = None   # (message type, crc ok, msgUsed)
        self.rtcm_types: dict[int, list[int]] = {}   # type -> [ok, used, failed] as the receiver reports
        self._time: str | None = None
        self._pos: dict = {"mode": MODE_NO_FIX}
        self._base: dict = {}
        self._tow: int | None = None                          # iTOW of the last NAV-PVT
        self._extra: dict[str, tuple[int, dict]] = {}         # name -> (iTOW, TPV keys)
        self._heading: float | None = None
        self._dop: dict = {}
        self._sats: dict[tuple[int, int], dict] = {}          # (gnssId, svId) from NAV-SAT
        self._sigs: list[dict] | None = None                  # from NAV-SIG, when it comes
        self._have_nav = False
        self._nmea_since: float | None = None
        self._warned = False

    def on_open(self) -> bytes:
        return b""                            # nothing is sent to the receiver

    # framing --------------------------------------------------------------
    def feed(self, data: bytes) -> list[Event]:
        self._buf += data
        events: list[Event] = []
        buf = self._buf
        while buf:
            first = buf[0]
            if first == SYNC1:
                if len(buf) < 2:
                    break
                if buf[1] == SYNC2:
                    status = self._ubx(events)
                    if status == "need":
                        break
                    continue
            elif first == 0x24:                          # '$': an NMEA sentence
                end = buf.find(b"\n")
                if end < 0:
                    if len(buf) < NMEA_MAX:
                        break
                else:
                    # printable ASCII up to CR LF.  A sentence that lost its end (UDP loss, a
                    # serial overrun) must not swallow the UBX frame after it, whose bytes
                    # are not text: it fails this test and the decoder steps on one byte.
                    raw_line = bytes(buf[:end + 1])
                    if (end < NMEA_MAX and NMEA_LINE.match(raw_line)
                            and raw_line[1:6].isalnum()):
                        del buf[:end + 1]
                        self._on_nmea(raw_line.decode("ascii").strip(), events)
                        continue
            elif first == rtcm.PREAMBLE:
                state, total = rtcm.check(buf)
                if state == "need":
                    break
                if state == "ok":
                    frame = bytes(buf[:total])
                    del buf[:total]
                    events.append(raw(f"RTCM3 {rtcm.message_type(frame)} {total} bytes",
                                      msg=f"RTCM {rtcm.message_type(frame)}"))
                    continue
            # something else: step over it up to the next byte that could start a frame
            nxt = len(buf)
            for marker in (SYNC1, 0x24, rtcm.PREAMBLE):
                i = buf.find(marker, 1)
                if 0 < i < nxt:
                    nxt = i
            del buf[:nxt]
        self._check_nav_seen(events)
        return events

    def _ubx(self, events: list[Event]) -> str:
        """Handle the buffer when it starts with B5 62.  Returns 'need' if more bytes are
        needed; otherwise the buffer has been advanced."""
        buf = self._buf
        if len(buf) < HEADER:
            return "need"
        length = buf[4] | buf[5] << 8
        if buf[2] not in CLASSES or length > MAX_PAYLOAD:
            del buf[:1]                                  # not a frame: look further on
            return "skip"
        total = HEADER + length + CK_LEN
        if len(buf) < total:
            return "need"
        if fletcher(buf[2:HEADER + length]) != (buf[HEADER + length], buf[HEADER + length + 1]):
            self.errors += 1
            del buf[:1]                                  # resynchronise on the next B5 62
            return "skip"
        msg_class, msg_id = buf[2], buf[3]
        payload = bytes(buf[HEADER:HEADER + length])
        del buf[:total]
        name = NAMES.get((msg_class, msg_id)) or f"{CLASSES[msg_class]} 0x{msg_id:02X}"
        text = f"UBX {name} 0x{msg_class:02X}{msg_id:02X} {length} bytes"
        handler = {(0x01, 0x07): self._nav_pvt, (0x01, 0x04): self._nav_dop,
                   (0x01, 0x35): self._nav_sat, (0x01, 0x43): self._nav_sig,
                   (0x01, 0x3C): self._nav_relposned,
                   (0x01, 0x01): self._nav_posecef, (0x01, 0x11): self._nav_velecef,
                   (0x01, 0x13): self._nav_hpposecef, (0x01, 0x14): self._nav_hpposllh,
                   (0x02, 0x32): self._rxm_rtcm, (0x02, 0x34): self._rxm_cor}.get(
                       (msg_class, msg_id))
        if handler is None:
            events.append(raw(text, msg=name))
            return "ok"
        try:
            extra = handler(payload)
        except struct.error:                             # shorter than its own layout
            events.append(raw(text + " (short)", "yellow"))
            return "ok"
        if extra is None:
            events.append(raw(text + " (ignored)", msg=name))
            return "ok"
        style = "dim"
        if (msg_class, msg_id) in ((0x02, 0x32), (0x02, 0x34)) and self.rtcm_last:
            msg_type, crc_ok, used = self.rtcm_last
            text += (f": RTCM {msg_type} " + ("crc ok" if crc_ok else "CRC FAILED")
                     + {1: ", not used", 2: ", used"}.get(used, ""))
            style = "dim" if crc_ok else "red"
        events.append(raw(text, style, msg=name))
        events.extend(extra)
        return "ok"

    def _on_nmea(self, line: str, events: list[Event]) -> None:
        events.append(raw(line, msg=line[1:6] if line.startswith("$") else ""))
        if self._nmea_since is None:
            self._nmea_since = self._clock()

    def _check_nav_seen(self, events: list[Event]) -> None:
        """NMEA arriving with no UBX NAV messages: say what to turn on, once."""
        if (self._nmea_since is not None and not self._have_nav and not self._warned
                and self._clock() - self._nmea_since >= NO_NAV_AFTER):
            self._warned = True
            events.append(note(
                "NMEA is arriving but no UBX NAV-PVT yet. gpsview does not use NMEA here: "
                "enable UBX-NAV-PVT, NAV-SAT and NAV-SIG on this port (CFG-MSGOUT in u-center).",
                "yellow"))

    # messages -------------------------------------------------------------
    def _nav_pvt(self, p: bytes) -> list[Event] | None:
        if len(p) < 92:
            return None
        self._have_nav = True
        (tow, year, month, day, hour, minute, sec, valid, _tacc, nano, fix_type, flags,
         _flags2, _nsv, lon, lat, height, hmsl, hacc, vacc, vel_n, vel_e, vel_d, gspeed,
         head_mot, sacc, head_acc, _pdop) = struct.unpack_from(
            "<IHBBBBBBIiBBBBiiiiIIiiiiiIIH", p)
        self._tow = tow
        mag_dec = struct.unpack_from("<h", p, 88)[0]
        if valid & 0x03 == 0x03:                         # validDate and validTime
            try:
                dt = (datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)
                      + timedelta(seconds=sec) + timedelta(microseconds=nano / 1000.0))
                self._time = iso8601(dt)
            except ValueError:
                pass
        if fix_type not in FIX_TYPE:
            self._pos = {"mode": MODE_NO_FIX}
        else:
            mode, status = FIX_TYPE[fix_type]
            if status != STATUS_TIME and flags & 0x02:   # differential or RTK: as gpsd does
                status = _status(flags)
            h_err, v_err = hacc / 1000.0, vacc / 1000.0
            self._pos = {
                "mode": mode, "status": status,
                "lat": lat * 1e-7, "lon": lon * 1e-7,
                "altHAE": height / 1000.0, "altMSL": hmsl / 1000.0,
                "speed": gspeed / 1000.0, "track": (head_mot * 1e-5) % 360.0,
                "climb": -vel_d / 1000.0,
                "epx": h_err, "epy": h_err, "epv": v_err, "eph": h_err,
                "sep": math.hypot(h_err, v_err),
                "eps": sacc / 1000.0, "epd": head_acc * 1e-5,
            }
            if valid & 0x08:                             # validMag
                self._pos["magvar"] = mag_dec * 1e-2
        return [self._tpv_event()]

    def _extra_event(self, name: str, tow: int, keys: dict) -> list[Event]:
        """Remember a position/velocity extra and, if it belongs to the current epoch, publish
        the TPV with it applied.  One that arrives before its NAV-PVT is published with it."""
        self._extra[name] = (tow, keys)
        return [self._tpv_event()] if tow == self._tow else []

    def _nav_posecef(self, p: bytes) -> list[Event] | None:
        if len(p) < 20:
            return None
        tow, x, y, z, acc = struct.unpack_from("<IiiiI", p)
        return self._extra_event("posecef", tow, {
            "ecefx": x * 1e-2, "ecefy": y * 1e-2, "ecefz": z * 1e-2, "ecefpAcc": acc * 1e-2})

    def _nav_velecef(self, p: bytes) -> list[Event] | None:
        if len(p) < 20:
            return None
        tow, vx, vy, vz, acc = struct.unpack_from("<IiiiI", p)
        return self._extra_event("velecef", tow, {
            "ecefvx": vx * 1e-2, "ecefvy": vy * 1e-2, "ecefvz": vz * 1e-2,
            "ecefvAcc": acc * 1e-2})

    def _nav_hpposecef(self, p: bytes) -> list[Event] | None:
        if len(p) < 28 or p[0] != 0:
            return None
        tow, x, y, z, xh, yh, zh, flags, acc = struct.unpack_from("<4xIiiibbbBI", p)
        if flags & 0x01:                                 # invalidEcef
            return []
        return self._extra_event("hpecef", tow, {
            "ecefx": x * 1e-2 + xh * 1e-4, "ecefy": y * 1e-2 + yh * 1e-4,
            "ecefz": z * 1e-2 + zh * 1e-4, "ecefpAcc": acc * 1e-4})

    def _nav_hpposllh(self, p: bytes) -> list[Event] | None:
        if len(p) < 36 or p[0] != 0:
            return None
        (flags, tow, lon, lat, height, hmsl, lon_h, lat_h, h_h, msl_h, hacc,
         vacc) = struct.unpack_from("<xxxBIiiiibbbbII", p)
        if flags & 0x01:                                 # invalidLlh
            return []
        h_err, v_err = hacc * 1e-4, vacc * 1e-4
        return self._extra_event("hpllh", tow, {
            "lat": lat * 1e-7 + lat_h * 1e-9, "lon": lon * 1e-7 + lon_h * 1e-9,
            "altHAE": height * 1e-3 + h_h * 1e-4, "altMSL": hmsl * 1e-3 + msl_h * 1e-4,
            "epx": h_err, "epy": h_err, "eph": h_err, "epv": v_err,
            "sep": math.hypot(h_err, v_err)})

    def _nav_dop(self, p: bytes) -> list[Event] | None:
        if len(p) < 18:
            return None
        _tow, g, pd, t, v, h = struct.unpack_from("<IHHHHH", p)
        self._dop = {k: x * 0.01 for k, x in
                     (("gdop", g), ("pdop", pd), ("tdop", t), ("vdop", v), ("hdop", h))
                     if x < 9999}
        return [self._sky_event()]

    def _nav_sat(self, p: bytes) -> list[Event] | None:
        if len(p) < 8 or p[4] != 1:
            return None
        self._have_nav = True
        count = p[5]
        if len(p) < 8 + 12 * count:
            return None
        sats: dict[tuple[int, int], dict] = {}
        for i in range(count):
            gnss, sv, cno, elev, azim, _res, flags = struct.unpack_from("<BBBbhhI", p, 8 + 12 * i)
            if not ubx_prn(gnss, sv):
                continue
            sats[(gnss, sv)] = {
                "cno": float(cno) if cno else None,
                "el": float(elev) if abs(elev) <= 90 else None,
                "az": float(azim % 360) if 0 <= azim <= 360 else None,
                "used": bool(flags & 0x08), "health": (flags >> 4) & 3,
            }
        self._sats = sats
        return [self._sky_event()]

    def _nav_sig(self, p: bytes) -> list[Event] | None:
        if len(p) < 8 or p[4] != 0:
            return None
        self._have_nav = True
        count = p[5]
        if len(p) < 8 + 16 * count:
            return None
        sigs = []
        for i in range(count):
            gnss, sv, sig, _freq, _res, cno, _qual, _corr, _iono, flags = struct.unpack_from(
                "<BBBBhBBBBH", p, 8 + 16 * i)
            if not ubx_prn(gnss, sv):
                continue
            sigs.append({"gnss": gnss, "sv": sv, "sig": sig, "cno": float(cno) if cno else None,
                         "used": bool(flags & 0x08), "health": flags & 3})
        self._sigs = sigs
        return [self._sky_event()]

    def _nav_relposned(self, p: bytes) -> list[Event] | None:
        if len(p) < 40:
            return None
        version = p[0]
        station = struct.unpack_from("<H", p, 2)[0]
        heading: float | None = None
        if version == 0:
            rel = struct.unpack_from("<iii", p, 8)
            hp = struct.unpack_from("<bbb", p, 20)
            flags = struct.unpack_from("<I", p, 36)[0]
            length = None
        else:
            if len(p) < 64:
                return None
            rel = struct.unpack_from("<iii", p, 8)
            length = struct.unpack_from("<i", p, 20)[0]
            head = struct.unpack_from("<i", p, 24)[0]
            hp = struct.unpack_from("<bbbb", p, 32)
            flags = struct.unpack_from("<I", p, 60)[0]
            if flags & 0x100:                            # relPosHeadingValid
                heading = (head * 1e-5) % 360.0
        carr = (flags >> 3) & 3                          # carrSoln: 0 none, 1 float, 2 fixed
        if not (flags & 0x01 and flags & 0x04):          # gnssFixOK and relPosValid
            self._base, self._heading = {}, None
        else:
            # centimetres plus a 0.1 mm high-precision part: (cm * 100 + hp) * 1e-4 m
            n, e, d = ((rel[i] * 100 + hp[i]) * 1e-4 for i in range(3))
            dist = ((length * 100 + hp[3]) * 1e-4 if length is not None
                    else math.sqrt(n * n + e * e + d * d))
            self._base = {
                "baseS": STATUS_RTK_FIX if carr == 2 else STATUS_RTK_FLT if carr == 1
                else STATUS_DGPS if flags & 0x02 else STATUS_GPS,
                "baseN": n, "baseE": e, "baseU": -d, "baseL": dist,
                "baseC": heading if heading is not None
                else math.degrees(math.atan2(e, n)) % 360.0,
                "dgpsSta": station,
            }
            self._heading = heading
        att: dict = {"class": "ATT", "device": self._device, **self._base}
        if self._time:
            att["time"] = self._time
        if self._heading is not None:
            att["heading"] = self._heading
        return [self._tpv_event(), obj(att)]

    def _rxm_rtcm(self, p: bytes) -> list[Event] | None:
        self.rtcm_last = None
        if len(p) < 8:
            return None
        flags = p[1]
        msg_type = struct.unpack_from("<H", p, 6)[0]
        crc_ok = not flags & 0x01
        used = (flags >> 1) & 3                          # 0 unknown, 1 not used, 2 used
        self._rtcm_report(msg_type, crc_ok, used)
        return []                                        # shown in the raw line, no state

    def _rxm_cor(self, p: bytes) -> list[Event] | None:
        """RXM-COR (F9 and later, which replaced RXM-RTCM): the receiver's verdict on each
        correction message it was sent: protocol, error status, used or not, message type."""
        self.rtcm_last = None
        if len(p) < 12 or p[0] != 1:
            return None
        status, msg_type = struct.unpack_from("<IH", p, 4)
        if status & 0x1f != 1:                           # not RTCM 3 (SPARTN, L6, ...)
            return []
        err, used_bits = (status >> 5) & 3, (status >> 7) & 3     # 0 unknown, 1 no error / not used
        type_valid = bool(status & 0x02000000)
        if err == 0:                                     # no verdict on the CRC: nothing to count
            return []
        # RXM-RTCM's msgUsed: 1 not used, 2 used (0 unknown); here the same two bits
        self._rtcm_report(msg_type if type_valid else 0, err == 1, used_bits)
        return []

    def _rtcm_report(self, msg_type: int, crc_ok: bool, used: int) -> None:
        if crc_ok:
            self.rtcm_ok += 1
        else:
            self.rtcm_failed += 1
        self.rtcm_last = (msg_type, crc_ok, used)
        if msg_type:
            row = self.rtcm_types.setdefault(msg_type, [0, 0, 0])
            row[0 if crc_ok else 2] += 1
            if crc_ok and used == 2:
                row[1] += 1

    # gpsd-shaped output ----------------------------------------------------
    def _tpv_event(self) -> Event:
        value: dict = {"class": "TPV", "device": self._device, **self._pos}
        if self._pos.get("mode", MODE_NO_FIX) > MODE_NO_FIX:
            # later entries are more precise and win: ECEF position, then its high-precision
            # version; the high-precision lat/lon/alt only with a fix to attach them to
            for name in ("posecef", "hpecef", "velecef", "hpllh"):
                tow, keys = self._extra.get(name, (None, {}))
                if tow == self._tow:
                    value.update(keys)
        if self._time:
            value["time"] = self._time
        value.update(self._base)
        return obj(value)

    def _sky_event(self) -> Event:
        rows: list[dict] = []
        with_signals: set[tuple[int, int]] = set()
        for s in self._sigs or []:
            key = (s["gnss"], s["sv"])
            with_signals.add(key)
            sat = self._sats.get(key, {})
            rows.append(self._row(s["gnss"], s["sv"], s["sig"], SIGNALS.get(
                (s["gnss"], s["sig"]), f"s{s['sig']}"), s["cno"], s["used"],
                s["health"] or sat.get("health", 0), sat))
        for (gnss, sv), sat in self._sats.items():
            if (gnss, sv) not in with_signals:           # NAV-SAT only: one row per satellite
                rows.append(self._row(gnss, sv, 0, "", sat["cno"], sat["used"],
                                      sat["health"], sat))
        value: dict = {"class": "SKY", "device": self._device, "satellites": rows}
        value.update(self._dop)
        return obj(value)

    @staticmethod
    def _row(gnss: int, sv: int, sig: int, signal: str, cno: float | None, used: bool,
             health: int, sat: dict) -> dict:
        row: dict = {"PRN": ubx_prn(gnss, sv), "gnssid": gnss, "svid": sv, "sigid": sig,
                     "used": used, "health": health}
        if signal:
            row["signal"] = signal
        if cno is not None:
            row["ss"] = cno
        if sat.get("el") is not None:
            row["el"] = sat["el"]
        if sat.get("az") is not None:
            row["az"] = sat["az"]
        return row


def ubx_feed(endpoint: Endpoint) -> Feed:
    return Feed(
        title="UBX",
        label=endpoint.label,
        header="UBX",
        make_transport=endpoint.make_transport,
        endpoint=endpoint,
        protocol="ubx",
        make_decoder=lambda: UbxDecoder(endpoint.device),
    )
