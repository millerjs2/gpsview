"""UBX decoder: framing, the message mappings, mixed streams, and real receiver captures.

Hand-built frames use the u-blox interface description's layouts.  Frames built with our own
encoder only prove we read the layout the way we wrote it, so the captures that ship with gpsd
(<gpsd source>/test/daemon/*.log, with gpsd's own decode in *.log.chk) are the independent check.
They are skipped when gpsd is not checked out.
"""
import json
import math
import os
import struct
from datetime import datetime, timezone

import pytest

from gpsview import rtcm
from gpsview.model import MODE_2D, MODE_3D, MODE_NO_FIX, GpsState, parse_time
from gpsview.ubx import UbxDecoder, fletcher, make_frame, ubx_prn

TOW = 123456000
GPSD_DAEMON = os.path.join(os.environ.get("GPSD_SRC", os.path.expanduser("~/dev/gpsd")),
                           "test", "daemon")


def pvt(fix_type=3, flags=0x01, valid=0x07, sec=9, nano=250_000_000, lat=37.5, lon=-122.25,
        height=42500, hmsl=8000, hacc=1800, vacc=3100, vel_d=-200, gspeed=1500,
        head_mot=4500000, mag_dec=-1300, tow=TOW):
    p = struct.pack("<IHBBBBBBIiBBBBiiiiIIiiiiiIIH", tow, 2026, 11, 10, 14, 5, sec, valid, 20,
                    nano, fix_type, flags, 0, 22, round(lon * 1e7), round(lat * 1e7), height,
                    hmsl, hacc, vacc, 1000, 500, vel_d, gspeed, head_mot, 120, 150000, 160)
    p += bytes(84 - len(p)) + struct.pack("<i", 0) + struct.pack("<hH", mag_dec, 0)
    assert len(p) == 92
    return make_frame(0x01, 0x07, p)


def sat_frame(sats):
    """sats: (gnssId, svId, cno, elev, azim, flags)"""
    p = struct.pack("<IBBH", TOW, 1, len(sats), 0)
    for g, sv, cno, el, az, flags in sats:
        p += struct.pack("<BBBbhhI", g, sv, cno, el, az, 0, flags)
    return make_frame(0x01, 0x35, p)


def sig_frame(sigs):
    """sigs: (gnssId, svId, sigId, cno, sigFlags)"""
    p = struct.pack("<IBBH", TOW, 0, len(sigs), 0)
    for g, sv, sig, cno, flags in sigs:
        p += struct.pack("<BBBBhBBBBHI", g, sv, sig, 0, 0, cno, 7, 0, 0, flags, 0)
    return make_frame(0x01, 0x43, p)


def relpos_v0(n_cm, e_cm, d_cm, hp=(0, 0, 0), flags=0x05 | 0x02, station=7):
    p = bytearray(40)
    p[2:4] = struct.pack("<H", station)
    p[4:8] = struct.pack("<I", TOW)
    p[8:20] = struct.pack("<iii", n_cm, e_cm, d_cm)
    p[20:23] = struct.pack("<bbb", *hp)
    p[36:40] = struct.pack("<I", flags)
    return make_frame(0x01, 0x3C, bytes(p))


def relpos_v1(n_cm, e_cm, d_cm, length_cm, heading, hp=(0, 0, 0, 0), flags=0x05 | 0x02,
              station=7):
    p = bytearray(64)
    p[0] = 1
    p[2:4] = struct.pack("<H", station)
    p[4:8] = struct.pack("<I", TOW)
    p[8:20] = struct.pack("<iii", n_cm, e_cm, d_cm)
    p[20:24] = struct.pack("<i", length_cm)
    p[24:28] = struct.pack("<i", heading)
    p[32:36] = struct.pack("<bbbb", *hp)
    p[60:64] = struct.pack("<I", flags)
    return make_frame(0x01, 0x3C, bytes(p))


def decode(*chunks: bytes, **kw):
    dec = UbxDecoder("test", **kw)
    st = GpsState()
    events = []
    for c in chunks:
        for ev in dec.feed(c):
            events.append(ev)
            if ev.kind == "obj":
                st.apply(ev.obj)
    return dec, st, events


def raws(events):
    return [e.text for e in events if e.kind == "raw"]


# ----------------------------------------------------------------- framing
def test_fletcher_checksum_matches_the_spec_example():
    # UBX-CFG-RST (cold start): B5 62 06 04 04 00 FF FF 00 00 0C 5D
    assert fletcher(bytes([0x06, 0x04, 0x04, 0x00, 0xFF, 0xFF, 0x00, 0x00])) == (0x0C, 0x5D)


def test_frames_split_across_reads_are_decoded():
    frame = pvt()
    dec, st, _ = decode(*(frame[i:i + 7] for i in range(0, len(frame), 7)))
    assert dec.errors == 0 and st.fix.mode == MODE_3D


def test_a_bad_checksum_is_counted_and_the_decoder_resynchronises():
    bad = bytearray(pvt())
    bad[-1] ^= 0xFF
    dec, st, events = decode(bytes(bad) + pvt(lat=10.0))
    assert dec.errors == 1
    assert st.fix.latitude == 10.0
    assert len(raws(events)) == 1


def test_garbage_and_a_false_sync_are_skipped_without_stalling():
    # B5 62 with a class that does not exist, and with a length larger than any message
    junk = b"\x00\xff\xb5\x62\x7f\x01\x10\x00" + b"\xb5\x62\x01\x07\xff\xff"
    dec, st, _ = decode(junk + pvt())
    assert st.fix.mode == MODE_3D
    assert dec.errors == 0


def test_a_giant_declared_length_does_not_wait_for_the_payload():
    dec, st, _ = decode(b"\xb5\x62\x01\x07\xff\xff" + pvt())
    assert st.fix.mode == MODE_3D


def test_unknown_messages_are_listed_not_decoded():
    _, _, events = decode(make_frame(0x0A, 0x04, b"\x00" * 40), make_frame(0x01, 0x03, b"\x00" * 16))
    assert raws(events) == ["UBX MON-VER 0x0A04 40 bytes", "UBX NAV-STATUS 0x0103 16 bytes"]


# ----------------------------------------------------------------- NAV-PVT
def test_nav_pvt_becomes_a_fix():
    _, st, events = decode(pvt(valid=0x0F))             # date, time, resolved, magnetic
    f = st.fix
    assert (f.mode, f.status) == (MODE_3D, 1)
    assert (f.latitude, f.longitude) == (37.5, -122.25)
    assert math.isclose(f.alt_hae, 42.5) and math.isclose(f.alt_msl, 8.0)
    assert math.isclose(f.speed, 1.5) and math.isclose(f.track, 45.0)
    assert math.isclose(f.climb, 0.2)
    assert math.isclose(f.eph, 1.8) and math.isclose(f.epv, 3.1)
    assert math.isclose(f.magnetic_var, -13.0)
    assert f.time == datetime(2026, 11, 10, 14, 5, 9, 250000, tzinfo=timezone.utc)
    assert raws(events) == ["UBX NAV-PVT 0x0107 92 bytes"]


def test_nav_pvt_time_survives_negative_nano_and_a_leap_second():
    _, st, _ = decode(pvt(sec=10, nano=-250_000_000))
    assert st.fix.time == datetime(2026, 11, 10, 14, 5, 9, 750000, tzinfo=timezone.utc)
    _, st, _ = decode(pvt(sec=60, nano=0))               # 23:59:60 style leap second
    assert st.fix.time == datetime(2026, 11, 10, 14, 6, 0, tzinfo=timezone.utc)


def test_nav_pvt_time_needs_valid_date_and_time():
    _, st, _ = decode(pvt(valid=0x01))
    assert st.fix.time is None


@pytest.mark.parametrize("fix_type, flags, mode, status", [
    (3, 0x01, MODE_3D, 1),
    (3, 0x03, MODE_3D, 2),                               # diffSoln: DGPS
    (3, 0x03 | 0x40, MODE_3D, 4),                        # carrSoln 1: RTK float
    (3, 0x03 | 0x80, MODE_3D, 3),                        # carrSoln 2: RTK fixed
    (2, 0x01, MODE_2D, 1),
    (1, 0x01, MODE_2D, 5),                               # dead reckoning
    (4, 0x01, MODE_3D, 6),                               # GNSS + dead reckoning
    (5, 0x01, MODE_3D, 7),                               # time only
])
def test_nav_pvt_fix_type_and_rtk_state_map_to_gpsd(fix_type, flags, mode, status):
    _, st, _ = decode(pvt(fix_type=fix_type, flags=flags))
    assert (st.fix.mode, st.fix.status) == (mode, status)


def test_nav_pvt_without_a_fix_has_no_position():
    _, st, _ = decode(pvt(fix_type=0, flags=0))
    assert st.fix.mode == MODE_NO_FIX and math.isnan(st.fix.latitude)


# ----------------------------------------------------------------- DOP, satellites, signals
def test_dops_and_satellites_in_one_sky_each_time():
    dop = make_frame(0x01, 0x04, struct.pack("<IHHHHHHH", TOW, 200, 160, 90, 120, 105, 80, 70))
    sats = sat_frame([(0, 1, 46, 60, 83, 0x08 | 0x10 | 7), (2, 5, 0, -5, 360, 0x20 | 1)])
    _, st, _ = decode(dop, sats)
    assert (st.dop.gdop, st.dop.pdop, st.dop.tdop) == (2.0, 1.6, 0.9)
    assert (st.dop.vdop, st.dop.hdop) == (1.2, 1.05)
    gps, gal = sorted(st.sats, key=lambda s: s.gnssid)
    assert (gps.prn, gps.ss, gps.elevation, gps.azimuth, gps.used) == (1, 46.0, 60.0, 83.0, True)
    assert gps.health == 1
    assert (gal.prn, gal.gnssid, gal.azimuth, gal.used, gal.health) == (305, 2, 0.0, False, 2)
    assert math.isnan(gal.ss)                            # cno 0 = not tracked
    # a later NAV-SAT must not blank the DOPs
    _, st2, _ = decode(dop, sats, sats)
    assert st2.dop.hdop == 1.05


def test_nav_sat_ignores_impossible_angles_and_unknown_satellites():
    sats = sat_frame([(0, 1, 40, 91, 400, 0x08), (6, 255, 30, 10, 10, 0x08), (9, 3, 30, 1, 1, 0)])
    _, st, _ = decode(sats)
    assert len(st.sats) == 1
    assert math.isnan(st.sats[0].elevation) and math.isnan(st.sats[0].azimuth)


def test_nav_sig_gives_one_row_per_signal_with_names_and_prused():
    sats = sat_frame([(0, 1, 46, 60, 83, 0x08 | 7), (2, 5, 39, 25, 344, 0x08 | 7),
                      (3, 8, 0, 15, 120, 7)])
    sigs = sig_frame([(0, 1, 0, 46, 0x08 | 1), (0, 1, 3, 40, 0x01),     # L1C/A used; L2CL not
                      (2, 5, 1, 39, 0x08 | 1)])                          # E1B
    _, st, _ = decode(sats, sigs)
    rows = sorted((s.gnssid, s.prn, s.signal, s.used, s.elevation) for s in st.sats)
    assert rows == [
        (0, 1, "L1CA", True, 60.0),
        (0, 1, "L2CL", False, 60.0),                     # geometry comes from NAV-SAT
        (2, 305, "E1B", True, 25.0),
        (3, 408, "", False, 15.0),                       # in NAV-SAT but not tracked
    ]
    assert {s.signal: s.ss for s in st.sats if s.signal} == {"L1CA": 46.0, "L2CL": 40.0, "E1B": 39.0}
    assert (st.sats_seen, st.sats_used) == (3, 2)        # satellites, not rows


@pytest.mark.parametrize("gnss, sv, prn", [
    (0, 7, 7), (1, 120, 33), (1, 151, 64), (1, 152, 152), (2, 5, 305), (3, 8, 408),
    (5, 2, 194), (6, 1, 65), (6, 24, 88), (7, 3, 803), (6, 255, 0), (0, 0, 0), (0, 40, 0),
])
def test_prn_numbering_is_gpsds(gnss, sv, prn):
    assert ubx_prn(gnss, sv) == prn


# ----------------------------------------------------------------- RTK
def test_relposned_v0_gives_the_base_line_in_metres_with_high_precision():
    # N 1234 cm + 0.5 mm; E -567 cm - 0.3 mm; D 89 cm + 0.1 mm; carrSoln 2 (fixed)
    frame = relpos_v0(1234, -567, 89, hp=(5, -3, 1), flags=0x01 | 0x02 | 0x04 | (2 << 3))
    _, st, events = decode(frame)
    b = st.fix.base
    assert (b.status, st.fix.dgps_station) == (3, 7)
    assert math.isclose(b.north, 12.3405) and math.isclose(b.east, -5.6703)
    assert math.isclose(b.up, -0.8901)
    assert math.isclose(b.length, math.sqrt(12.3405 ** 2 + 5.6703 ** 2 + 0.8901 ** 2))
    assert math.isclose(b.course, math.degrees(math.atan2(-5.6703, 12.3405)) % 360)
    assert st.attitude.base.status == 3
    assert raws(events) == ["UBX NAV-RELPOSNED 0x013C 40 bytes"]


def test_relposned_v1_uses_its_own_length_and_heading():
    frame = relpos_v1(100, 0, 0, 105, 9000000, hp=(0, 0, 0, 40),
                      flags=0x01 | 0x04 | 0x100 | (1 << 3))     # float, heading valid
    _, st, _ = decode(frame)
    b = st.fix.base
    assert b.status == 4
    assert math.isclose(b.length, 1.054)                 # 105 cm + 40 * 0.1 mm
    assert math.isclose(b.course, 90.0)                  # relPosHeading, not atan2(0, 1)
    assert math.isclose(st.attitude.heading, 90.0)


def test_relposned_without_a_valid_solution_clears_the_base():
    _, st, _ = decode(relpos_v0(100, 100, 0, flags=0x05 | (2 << 3)), relpos_v0(0, 0, 0, flags=0x01))
    assert st.fix.base.status == 0 and math.isnan(st.fix.base.length)


def test_a_position_after_the_base_line_keeps_the_base_line():
    _, st, _ = decode(relpos_v0(100, 100, 0, flags=0x05 | (2 << 3)), pvt())
    assert st.fix.base.status == 3 and st.fix.mode == MODE_3D


def test_rxm_rtcm_reports_what_the_receiver_did_with_the_corrections():
    def frame(flags, msg_type):
        return make_frame(0x02, 0x32, struct.pack("<BBHHH", 2, flags, 0, 5, msg_type))
    dec, _, events = decode(frame(0x04, 1077), frame(0x01, 1087))      # used; then CRC failed
    assert (dec.rtcm_ok, dec.rtcm_failed) == (1, 1)
    assert dec.rtcm_last == (1087, False, 0)
    texts = [e for e in events if e.kind == "raw"]
    assert texts[0].text.endswith("RTCM 1077 crc ok, used") and texts[0].style == "dim"
    assert "CRC FAILED" in texts[1].text and texts[1].style == "red"


# ----------------------------------------------------------------- mixed streams
def test_nmea_and_rtcm_in_the_stream_are_shown_not_counted_as_errors():
    gga = b"$GNGGA,140509.00,3730.0000,N,12215.0000,W,1,22,0.9,42.5,M,-34.0,M,,*5B\r\n"
    rtcm_frame = rtcm.make_frame(bytes([0x43, 0x50]) + bytes(20))     # type 1077
    dec, st, events = decode(gga, rtcm_frame, pvt(), gga)
    assert dec.errors == 0 and st.fix.mode == MODE_3D
    assert raws(events) == [gga.decode().strip(), "RTCM3 1077 28 bytes",
                            "UBX NAV-PVT 0x0107 92 bytes", gga.decode().strip()]
    assert st.fix.latitude == 37.5                       # NMEA position did not replace UBX


def test_a_truncated_nmea_line_does_not_swallow_the_ubx_frame_after_it():
    # the sentence lost its end, and the frame after it has a 0x0A byte inside
    frame = pvt(nano=0x0A0A0A0A, lat=11.0)
    assert b"\n" in frame
    dec, st, events = decode(b"$GNGGA,140509.00,3730.0000,N,1221" + frame + pvt(lat=12.0))
    assert [t for t in raws(events) if "NAV-PVT" in t] == ["UBX NAV-PVT 0x0107 92 bytes"] * 2
    assert st.fix.latitude == 12.0


def test_nmea_without_nav_pvt_explains_what_to_enable_once():
    now = [100.0]
    gga = b"$GNGGA,140509.00,3730.0000,N,12215.0000,W,1,22,0.9,42.5,M,-34.0,M,,*5B\r\n"
    dec = UbxDecoder("t", clock=lambda: now[0])
    assert [e for e in dec.feed(gga) if e.kind == "note"] == []
    now[0] += 6
    notes = [e for e in dec.feed(gga) if e.kind == "note"]
    assert len(notes) == 1 and "NAV-PVT" in notes[0].text
    assert [e for e in dec.feed(gga) if e.kind == "note"] == []      # only once


def test_no_warning_when_nav_pvt_is_arriving():
    now = [100.0]
    gga = b"$GNGGA,140509.00,3730.0000,N,12215.0000,W,1,22,0.9,42.5,M,-34.0,M,,*5B\r\n"
    dec = UbxDecoder("t", clock=lambda: now[0])
    dec.feed(gga + pvt())
    now[0] += 60
    assert [e for e in dec.feed(gga) if e.kind == "note"] == []


def test_the_decoder_sends_nothing():
    assert UbxDecoder("t").on_open() == b""


# ----------------------------------------------------------------- real captures (gpsd)
def load_capture(name: str) -> bytes:
    path = os.path.join(GPSD_DAEMON, name)
    if not os.path.exists(path):
        pytest.skip(f"{path} not found")
    data = open(path, "rb").read()
    i = 0
    while data[i:i + 1] == b"#":                         # comment header
        i = data.index(b"\n", i) + 1
    return data[i:]


def gpsd_tpv_and_sky(name: str):
    path = os.path.join(GPSD_DAEMON, name + ".chk")
    objs = [json.loads(line) for line in open(path) if line.startswith("{")]
    return ([o for o in objs if o["class"] == "TPV"], [o for o in objs if o["class"] == "SKY"])


def run_capture(name: str):
    data = load_capture(name)
    dec = UbxDecoder("cap")
    tpvs, skies = [], []
    for i in range(0, len(data), 333):                   # arbitrary read sizes
        for ev in dec.feed(data[i:i + 333]):
            if ev.kind == "obj" and ev.obj["class"] == "TPV":
                tpvs.append(ev.obj)
            elif ev.kind == "obj" and ev.obj["class"] == "SKY":
                skies.append(ev.obj)
    return dec, tpvs, skies


# u-blox 8, ZED-F9P (with RTCM output mixed in), ZED-F9R, NEO-F9V, and a multi-GNSS NAV-SIG capture
CAPTURES = ["ublox-neo-m8p.log", "ublox-zed-f9p-rtk.log", "ublox-zed-f9r.log",
            "ublox-neo-f9v.log", "ublox-f9p-rtcm.log"]


@pytest.mark.parametrize("name", CAPTURES)
def test_real_capture_positions_match_gpsd(name):
    dec, tpvs, _ = run_capture(name)
    gpsd_tpv, _ = gpsd_tpv_and_sky(name)
    assert dec.errors == 0
    mine = {parse_time(t["time"]).replace(microsecond=0): t for t in tpvs if "time" in t}
    theirs = {parse_time(t["time"]).replace(microsecond=0): t for t in gpsd_tpv if "time" in t}
    common = mine.keys() & theirs.keys()
    assert len(common) >= max(1, len(theirs) - 1), "decoded too few epochs"
    for when in common:
        a, b = mine[when], theirs[when]
        assert a["mode"] == b["mode"]
        if b["mode"] >= MODE_2D:
            assert math.isclose(a["lat"], b["lat"], abs_tol=2e-7)
            assert math.isclose(a["lon"], b["lon"], abs_tol=2e-7)
            assert math.isclose(a["altHAE"], b["altHAE"], abs_tol=0.1)   # gpsd truncates to 0.1 m
            assert math.isclose(a["speed"], b["speed"], abs_tol=2e-3)
            if "status" in b:
                assert a["status"] == b["status"], when


def frames_of(data: bytes, msg_class: int, msg_id: int) -> bytes:
    """Only the good frames of one message type from a capture."""
    out, i = [], 0
    while True:
        i = data.find(b"\xb5\x62", i)
        if i < 0 or i + 8 > len(data):
            return b"".join(out)
        n = data[i + 4] | data[i + 5] << 8
        frame = data[i:i + 8 + n]
        if len(frame) == 8 + n and fletcher(frame[2:-2]) == (frame[-2], frame[-1]):
            if (frame[2], frame[3]) == (msg_class, msg_id):
                out.append(frame)
            i += 8 + n
        else:
            i += 1


def sat_key(s):
    return (s["gnssid"], s["PRN"], s["used"], s.get("ss", 0.0), s.get("el"), s.get("az"))


@pytest.mark.parametrize("name", CAPTURES)
def test_real_capture_nav_sat_matches_gpsd(name):
    """NAV-SAT alone: the same satellites, used flags, C/N0, elevation and azimuth as gpsd."""
    data = load_capture(name)
    dec = UbxDecoder("cap")
    skies = [e.obj for e in dec.feed(frames_of(data, 0x01, 0x35)) if e.kind == "obj"]
    _, gpsd_sky = gpsd_tpv_and_sky(name)
    n = len(skies[-1]["satellites"])
    theirs = [s for s in gpsd_sky if len(s.get("satellites", ())) == n]   # NAV-SAT based skies
    assert skies and theirs
    mine = {frozenset(sat_key(s) for s in sky["satellites"]) for sky in skies}
    assert frozenset(sat_key(s) for s in theirs[-1]["satellites"]) in mine, name


@pytest.mark.parametrize("name", ["ublox-zed-f9r.log", "ublox-neo-f9v.log", "ublox-f9p-rtcm.log"])
def test_real_capture_nav_sig_matches_gpsd(name):
    """NAV-SIG: the same signals (satellite and C/N0) as gpsd's NAV-SIG skyview."""
    data = load_capture(name)
    dec = UbxDecoder("cap")
    skies = [e.obj for e in dec.feed(frames_of(data, 0x01, 0x43)) if e.kind == "obj"]
    _, gpsd_sky = gpsd_tpv_and_sky(name)

    def key(s):
        return (s["gnssid"], s["PRN"], s.get("ss", 0.0))
    last_sig_sky = [s for s in gpsd_sky if s.get("satellites")][-1]
    theirs = sorted(key(s) for s in last_sig_sky["satellites"])
    assert any(sorted(key(s) for s in sky["satellites"]) == theirs for sky in skies), name
    assert all(s["signal"] for sky in skies for s in sky["satellites"])


def test_real_m8p_capture_dops_match_gpsd():
    _, _, skies = run_capture("ublox-neo-m8p.log")
    _, gpsd_sky = gpsd_tpv_and_sky("ublox-neo-m8p.log")
    last = gpsd_sky[-1]
    keys = ("gdop", "hdop", "pdop", "tdop", "vdop")
    assert any(all(k in sky and math.isclose(sky[k], last[k], abs_tol=1e-9) for k in keys) for sky in skies)


def test_real_f9p_capture_with_rtcm_in_the_stream_has_no_errors():
    dec, tpvs, _ = run_capture("ublox-f9p-rtcm.log")
    assert dec.errors == 0 and tpvs


def test_real_m8p_capture_reports_rxm_rtcm():
    dec, _, _ = run_capture("ublox-neo-m8p.log")
    assert dec.rtcm_ok + dec.rtcm_failed == 12           # twelve 0x02 0x32 messages in the file


# ----------------------------------------------------------------- ECEF and high precision
def hpllh(lat=37.5, lon=-122.25, lat_hp=-7, lon_hp=5, height=42500, h_hp=3, hmsl=8000, msl_hp=-4,
          hacc=125, vacc=210, flags=0, tow=None):
    p = struct.pack("<BBBBIiiiibbbbII", 0, 0, 0, flags, TOW if tow is None else tow,
                    round(lon * 1e7), round(lat * 1e7), height, hmsl, lon_hp, lat_hp, h_hp, msl_hp,
                    hacc, vacc)
    assert len(p) == 36
    return make_frame(0x01, 0x14, p)


def hpecef(x=-270123456, y=-421012346, z=388012345, xh=4, yh=-3, zh=9, flags=0,
           acc=135, tow=None):
    p = struct.pack("<BBBBIiiibbbBI", 0, 0, 0, 0, TOW if tow is None else tow, x, y, z, xh, yh, zh,
                    flags, acc)
    assert len(p) == 28
    return make_frame(0x01, 0x13, p)


def posecef(x=-270123456, y=-421012345, z=388012345, acc=190, tow=None):
    return make_frame(0x01, 0x01, struct.pack("<IiiiI", TOW if tow is None else tow, x, y, z, acc))


def velecef(vx=-24, vy=-3, vz=19, acc=105, tow=None):
    return make_frame(0x01, 0x11, struct.pack("<IiiiI", TOW if tow is None else tow, vx, vy, vz, acc))


def last_tpv(data):
    dec = UbxDecoder("t")
    tpvs = [e.obj for e in dec.feed(data) if e.kind == "obj" and e.obj["class"] == "TPV"]
    return tpvs[-1]


def test_high_precision_llh_replaces_the_pvt_position():
    t = last_tpv(pvt() + hpllh())
    assert math.isclose(t["lat"], 37.5 - 7e-9, abs_tol=1e-12)
    assert math.isclose(t["lon"], -122.25 + 5e-9, abs_tol=1e-12)
    assert math.isclose(t["altHAE"], 42.5 + 3e-4, abs_tol=1e-9)         # mm plus 0.1 mm
    assert math.isclose(t["altMSL"], 8.0 - 4e-4, abs_tol=1e-9)
    assert math.isclose(t["eph"], 0.0125) and math.isclose(t["epv"], 0.021)      # 0.1 mm units
    assert t["mode"] == MODE_3D and t["speed"] > 0                       # the rest is from PVT


def test_high_precision_arriving_before_its_pvt_is_still_applied():
    t = last_tpv(hpllh() + pvt())
    assert math.isclose(t["lat"], 37.5 - 7e-9, abs_tol=1e-12)


def test_a_stale_high_precision_message_is_not_mixed_into_a_newer_fix():
    assert last_tpv(hpllh(tow=TOW - 1000) + pvt())["lat"] == pytest.approx(37.5, abs=1e-12)
    t = last_tpv(pvt() + hpllh() + hpecef() + pvt(lat=37.6, tow=TOW + 1000))
    assert t["lat"] == pytest.approx(37.6, abs=1e-12) and "ecefx" not in t      # the old epoch's


def test_invalid_and_short_high_precision_messages_are_ignored():
    assert last_tpv(pvt() + hpllh(flags=0x01))["lat"] == pytest.approx(37.5, abs=1e-12)
    assert last_tpv(pvt() + hpecef(flags=0x01)).get("ecefx") is None
    short = make_frame(0x01, 0x14, bytes(20))
    dec = UbxDecoder("t")
    ev = dec.feed(pvt() + short)
    assert any("(ignored)" in e.text for e in ev if e.kind == "raw")


def test_no_fix_gets_no_extras():
    t = last_tpv(pvt(fix_type=0, flags=0) + hpllh() + hpecef())
    assert t["mode"] == MODE_NO_FIX and "ecefx" not in t and t.get("lat") is None


def test_ecef_position_and_velocity():
    t = last_tpv(pvt() + posecef() + velecef())
    assert t["ecefx"] == pytest.approx(-2701234.56) and t["ecefz"] == pytest.approx(3880123.45)
    assert t["ecefpAcc"] == pytest.approx(1.9)
    assert (t["ecefvx"], t["ecefvy"], t["ecefvz"]) == pytest.approx((-0.24, -0.03, 0.19))
    assert t["ecefvAcc"] == pytest.approx(1.05)


def test_high_precision_ecef_wins_over_plain_ecef_whichever_comes_first():
    for order in (pvt() + posecef() + hpecef(), pvt() + hpecef() + posecef()):
        t = last_tpv(order)
        assert t["ecefx"] == pytest.approx(-2701234.56 + 4e-4, abs=1e-6)
        assert t["ecefy"] == pytest.approx(-421012346 * 1e-2 - 3e-4, abs=1e-6)
        assert t["ecefz"] == pytest.approx(388012345 * 1e-2 + 9e-4, abs=1e-6)
        assert t["ecefpAcc"] == pytest.approx(0.0135)


def test_the_stats_view_shows_the_ecef_rows_and_the_extra_digits():
    from datetime import datetime, timezone
    from gpsview import views
    from gpsview.fmt import DD
    state = GpsState()
    for e in UbxDecoder("t").feed(pvt() + hpllh() + hpecef() + velecef()):
        if e.kind == "obj":
            state.apply(e.obj)
    opts = views.ViewOpts(units=views.UNITS["m"], deg=DD, magnetic=False)
    lines = views.gnss_lines(state, opts, online=True, secs=1, now=datetime.now(timezone.utc))
    ecef = [ln for ln in lines if ln.lstrip().startswith("ECEF")]
    assert len(ecef) == 3 and not any("n/a" in ln for ln in ecef), ecef
    assert "-2701234.5596" in ecef[0] and "-0.240" in ecef[0]      # 0.1 mm kept
    lat = [ln for ln in lines if ln.lstrip().startswith("Latitude")][0]
    assert "37.49999999" in lat                                  # 37.5 - 7e-9: the extra digits


def test_real_high_precision_capture_matches_gpsd():
    """ZED-F9P with NAV-HPPOSLLH, NAV-HPPOSECEF and NAV-VELECEF: gpsd's own decode is the oracle."""
    name = "ublox-zed-f9p-hp.log"
    dec, tpvs, _ = run_capture(name)
    gpsd_tpv, _ = gpsd_tpv_and_sky(name)
    mine = {}
    for t in tpvs:                                        # the last TPV of each second has everything
        if "time" in t:
            mine[parse_time(t["time"]).replace(microsecond=0)] = t
    theirs = {parse_time(t["time"]).replace(microsecond=0): t for t in gpsd_tpv if "time" in t}
    common = [w for w in mine.keys() & theirs.keys() if theirs[w].get("ecefx") is not None]
    assert len(common) >= 10
    for when in common:
        a, b = mine[when], theirs[when]
        for key in ("ecefx", "ecefy", "ecefz"):
            assert a[key] == pytest.approx(b[key], abs=2e-4), (when, key)       # to 0.1 mm
        for key in ("ecefvx", "ecefvy", "ecefvz", "ecefvAcc"):
            assert a[key] == pytest.approx(b[key], abs=6e-3), (when, key)
        assert a["ecefpAcc"] == pytest.approx(b["ecefpAcc"], abs=2e-4)
        assert a["lat"] == pytest.approx(b["lat"], abs=2e-9)                     # 1e-9 degree
        assert a["lon"] == pytest.approx(b["lon"], abs=2e-9)
        assert a["altHAE"] == pytest.approx(b["altHAE"], abs=1.5e-3)             # gpsd scales the
        assert a["altMSL"] == pytest.approx(b["altMSL"], abs=1.5e-3)             # 0.1 mm digit by 1e-5


# ----------------------------------------------------------------- RXM-COR (F9: RTCM verdicts)
def rxm_cor(msg_type=1077, proto=1, err=1, used=2, valid=True, version=1):
    status = proto | err << 5 | used << 7 | (0x02000000 if valid else 0)
    return make_frame(0x02, 0x34, struct.pack("<BBHIHH", version, 0, 0, status, msg_type, 0))


def test_rxm_cor_reports_what_the_receiver_did_with_each_rtcm_message():
    dec = UbxDecoder("t")
    ev = dec.feed(rxm_cor(1077) + rxm_cor(1077) + rxm_cor(1230, used=1) + rxm_cor(1087, err=2))
    assert (dec.rtcm_ok, dec.rtcm_failed) == (3, 1)
    assert dec.rtcm_types == {1077: [2, 2, 0], 1230: [1, 0, 0], 1087: [0, 0, 1]}
    lines = [e.text for e in ev if e.kind == "raw"]
    assert lines[0].endswith("RXM-COR 0x0234 12 bytes: RTCM 1077 crc ok, used"), lines[0]
    assert "not used" in lines[2] and "CRC FAILED" in lines[3]


def test_rxm_cor_ignores_other_protocols_unknown_status_and_other_versions():
    dec = UbxDecoder("t")
    ev = dec.feed(rxm_cor(1077, proto=2) + rxm_cor(1077, err=0) + rxm_cor(1077, version=2))
    assert (dec.rtcm_ok, dec.rtcm_failed, dec.rtcm_types) == (0, 0, {})
    assert not any("RTCM 1077" in e.text for e in ev if e.kind == "raw")      # no stale verdict


def test_rxm_cor_without_a_valid_type_counts_but_has_no_type_row():
    dec = UbxDecoder("t")
    dec.feed(rxm_cor(0, valid=False))
    assert dec.rtcm_ok == 1 and dec.rtcm_types == {}


def test_real_f9p_rxm_cor_frames():
    """Frames from a ZED-F9P being fed RTCM 3: all RTCM3, no error, used."""
    path = os.path.join(os.path.dirname(__file__), "data", "zed-f9p-rxm-cor.ubx")
    dec = UbxDecoder("t")
    ev = dec.feed(open(path, "rb").read())
    assert dec.errors == 0 and dec.rtcm_ok == 12 and dec.rtcm_failed == 0
    assert all(used == ok for ok, used, bad in dec.rtcm_types.values())
    assert {1077, 1087} <= dec.rtcm_types.keys()
    assert all("RXM-COR" in e.text for e in ev if e.kind == "raw") and ev
