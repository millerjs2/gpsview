"""SBP decoder: framing and CRC, then the mapping to gpsd-shaped objects.

Frames are built with the sbp package's own encoder, so these tests check what the decoder
does with real SBP, not with bytes written by hand (apart from the unknown-type frame).
"""
import binascii
import math

from sbp.navigation import MsgBaselineNED, MsgDops, MsgPosLLH, MsgUtcTime, MsgVelNED
from sbp.observation import MsgSvAzEl
from sbp.tracking import MsgMeasurementState

from gpsview.model import MODE_3D, MODE_NO_FIX, MODE_NOT_SEEN, GpsState
from gpsview.swift import SbpDecoder

TOW = 123456


def pos(flags=6, **kw):
    fields = dict(sender=1, tow=TOW, lat=37.5, lon=-122.25, height=42.5,
                  h_accuracy=18, v_accuracy=31, n_sats=8, flags=flags)
    fields.update(kw)
    return MsgPosLLH(**fields).to_binary()


def decode(*frames: bytes):
    dec = SbpDecoder("test")
    st = GpsState()
    events = []
    for f in frames:
        for ev in dec.feed(f):
            events.append(ev)
            if ev.kind == "obj":
                st.apply(ev.obj)
    return dec, st, events


def raws(events):
    return [e.text for e in events if e.kind == "raw"]


def test_pos_llh_becomes_a_fix():
    dec, st, events = decode(pos(flags=6))
    assert st.fix.mode == MODE_3D
    assert st.fix.status == 2                       # SBAS position is shown as DGPS
    assert (st.fix.latitude, st.fix.longitude) == (37.5, -122.25)
    assert st.fix.alt_hae == 42.5
    assert math.isclose(st.fix.epx, 0.018) and math.isclose(st.fix.epv, 0.031)
    assert math.isclose(st.fix.sep, math.hypot(0.018, 0.031))
    assert st.leap_seconds is None                  # SBP here has no leap seconds
    assert raws(events) == ["SBP PosLLH 0x020A sender 1 34 bytes"]


def test_fix_flags_map_to_gpsd_status():
    _, st, _ = decode(pos(flags=4))                 # fixed RTK
    assert st.fix.status == 3
    _, st, _ = decode(pos(flags=3))                 # float RTK
    assert st.fix.status == 4


def test_flags_zero_is_no_fix_with_no_position():
    _, st, _ = decode(pos(flags=0))
    assert st.fix.mode == MODE_NO_FIX
    assert math.isnan(st.fix.latitude)


def test_utc_time_becomes_the_fix_time():
    utc = MsgUtcTime(sender=1, flags=0x11, tow=TOW, year=2026, month=11, day=10,
                     hours=14, minutes=5, seconds=9, ns=250_000_000).to_binary()
    _, st, _ = decode(utc, pos())
    assert st.fix.time.isoformat() == "2026-11-10T14:05:09.250000+00:00"


def test_utc_with_no_time_source_is_ignored():
    utc = MsgUtcTime(sender=1, flags=0, tow=TOW, year=2026, month=11, day=10,
                     hours=14, minutes=5, seconds=9, ns=0).to_binary()
    _, st, _ = decode(utc, pos())
    assert st.fix.time is None


def test_dops_are_scaled_by_hundredths():
    _, st, _ = decode(MsgDops(sender=1, tow=TOW, gdop=200, pdop=160, tdop=90, hdop=105,
                              vdop=120, flags=1).to_binary())
    assert (st.dop.pdop, st.dop.hdop, st.dop.vdop, st.dop.tdop, st.dop.gdop) == \
        (1.6, 1.05, 1.2, 0.9, 2.0)


def test_sv_azel_gives_azimuth_in_degrees_not_half_degrees():
    azel = MsgSvAzEl(sender=1, azel=[{"sid": {"sat": 5, "code": 0}, "az": 80, "el": 40}])
    _, st, _ = decode(azel.to_binary())
    s = st.sats[0]
    assert (s.prn, s.gnssid, s.elevation, s.azimuth) == (5, 0, 40.0, 160.0)
    assert s.used is False and math.isnan(s.ss)     # no measurement yet


def test_measurement_state_gives_snr_in_dbhz_and_marks_tracking_as_used():
    azel = MsgSvAzEl(sender=1, azel=[{"sid": {"sat": 5, "code": 0}, "az": 80, "el": 40},
                                     {"sid": {"sat": 9, "code": 0}, "az": 10, "el": 5}])
    meas = MsgMeasurementState(sender=1, states=[
        {"mesid": {"sat": 5, "code": 0}, "cn0": 180}])     # 45 dB-Hz
    _, st, _ = decode(azel.to_binary(), meas.to_binary())
    by_prn = {s.prn: s for s in st.sats}
    assert by_prn[5].ss == 45.0 and by_prn[5].used is True
    assert by_prn[9].used is False
    assert (st.sats_seen, st.sats_used) == (2, 1)


def test_glonass_slot_becomes_prn_64_plus_slot():
    azel = MsgSvAzEl(sender=1, azel=[{"sid": {"sat": 5, "code": 3}, "az": 1, "el": 2},
                                     {"sid": {"sat": 120, "code": 3}, "az": 1, "el": 2}])
    _, st, _ = decode(azel.to_binary())
    assert [(s.gnssid, s.prn) for s in st.sats] == [(6, 69)]


def test_galileo_and_beidou_prns_follow_the_nmea_numbering():
    # gpsd's NMEA driver numbers Galileo 300 + svid and BeiDou 400 + svid, so the same
    # satellite has the same PRN from either source (checked against a live receiver)
    azel = MsgSvAzEl(sender=1, azel=[{"sid": {"sat": 2, "code": 14}, "az": 10, "el": 5},
                                     {"sid": {"sat": 14, "code": 12}, "az": 20, "el": 6}])
    _, st, _ = decode(azel.to_binary())
    assert sorted((s.gnssid, s.prn, s.signal) for s in st.sats) == [(2, 302, "E1B"),
                                                                    (3, 414, "B1")]


def test_unknown_codes_are_not_listed():
    azel = MsgSvAzEl(sender=1, azel=[{"sid": {"sat": 5, "code": 99}, "az": 1, "el": 2}])
    _, st, _ = decode(azel.to_binary())
    assert st.sats == ()


def test_baseline_becomes_base_in_tpv_and_att():
    base = MsgBaselineNED(sender=1, tow=TOW, n=3000, e=4000, d=-1200, h_accuracy=10,
                          v_accuracy=20, n_sats=8, flags=4).to_binary()
    _, st, events = decode(pos(), base)
    b = st.fix.base
    assert b.status == 3                            # fixed RTK
    assert math.isclose(b.north, 3.0) and math.isclose(b.east, 4.0)
    assert math.isclose(b.up, 1.2)                  # NED down becomes up
    assert math.isclose(b.length, math.sqrt(3.0 ** 2 + 4.0 ** 2 + 1.2 ** 2))
    assert math.isclose(b.course, math.degrees(math.atan2(4.0, 3.0)))
    assert st.attitude is not None and st.attitude.base.status == 3
    assert math.isnan(st.attitude.heading)          # SBP here gives no heading


def test_velocity_gives_speed_and_track():
    vel = MsgVelNED(sender=1, tow=TOW, n=3000, e=4000, d=-500, h_accuracy=1, v_accuracy=1,
                    n_sats=8, flags=2).to_binary()
    # VEL_NED is merged into the TPV that POS_LLH emits
    _, st, _ = decode(vel, pos())
    assert math.isclose(st.fix.speed, 5.0)
    assert math.isclose(st.fix.track, math.degrees(math.atan2(4000, 3000)) % 360)
    assert math.isclose(st.fix.climb, 0.5)


def test_a_bad_crc_is_counted_and_the_next_frame_still_decodes():
    bad = bytearray(pos())
    bad[10] ^= 0xFF                                  # corrupt a payload byte
    dec, st, events = decode(bytes(bad), pos(flags=0))
    assert dec.errors == 1
    assert st.fix.mode == MODE_NO_FIX                # the good frame after it was used


def test_frames_split_across_reads_are_joined():
    data = pos() + pos(flags=4)
    dec = SbpDecoder("test")
    events = []
    for i in range(len(data)):                       # one byte at a time
        events.extend(dec.feed(data[i:i + 1]))
    assert [e.obj["mode"] for e in events if e.kind == "obj"] == [3, 3]
    assert dec.errors == 0


def test_garbage_before_a_frame_is_skipped():
    dec = SbpDecoder("test")
    events = dec.feed(b"\x00\x01junk" + pos())
    assert any(e.kind == "obj" for e in events)
    assert dec.errors == 0


def test_unknown_message_type_is_shown_but_not_decoded():
    payload = b"\x01\x02\x03"
    body = (0x1234).to_bytes(2, "little") + (7).to_bytes(2, "little") + bytes([len(payload)]) + payload
    frame = b"\x55" + body + binascii.crc_hqx(body, 0).to_bytes(2, "little")
    dec, st, events = decode(frame)
    assert raws(events) == ["SBP unknown 0x1234 sender 7 3 bytes"]
    assert st.fix.mode == MODE_NOT_SEEN and dec.errors == 0   # nothing usable was decoded


def test_the_decoder_never_writes_to_the_receiver():
    assert SbpDecoder("test").on_open() == b""


def test_a_satellite_on_two_signals_is_two_rows_but_counted_once():
    azel = MsgSvAzEl(sender=1, azel=[{"sid": {"sat": 1, "code": 0}, "az": 80, "el": 40}])
    meas = MsgMeasurementState(sender=1, states=[
        {"mesid": {"sat": 1, "code": 0}, "cn0": 180},     # L1CA
        {"mesid": {"sat": 1, "code": 1}, "cn0": 168}])    # L2CM, no geometry of its own
    _, st, _ = decode(azel.to_binary(), meas.to_binary())
    rows = sorted((s.signal, s.elevation, s.ss) for s in st.sats)
    assert rows == [("L1CA", 40.0, 45.0), ("L2CM", 40.0, 42.0)]   # borrowed elevation
    assert (st.sats_seen, st.sats_used) == (1, 1)
