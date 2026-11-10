"""gpsd JSON -> GpsState.  Samples are taken from gpsd's man/gpsd_json.adoc."""
import json
import math
from datetime import timezone

from gpsview.model import (MODE_3D, MODE_NOT_SEEN, SAT_HEALTH_BAD, GpsState,
                           parse_time)

TPV = ('{"class":"TPV","device":"/dev/pts/1",'
       '"time":"2005-06-08T10:34:48.283Z","ept":0.005,'
       '"lat":46.498293369,"lon":7.567411672,"alt":1343.127,'
       '"eph":36.000,"epv":32.321,'
       '"track":10.3788,"speed":0.091,"climb":-0.085,"mode":3,"leapseconds":18}')

SKY = ('{"class":"SKY","device":"/dev/pts/1",'
       '"time":"2005-07-08T11:28:07.114Z",'
       '"xdop":1.55,"hdop":1.24,"pdop":1.99,'
       '"satellites":['
       '{"PRN":23,"el":6,"az":84,"ss":0,"used":false},'
       '{"PRN":28,"el":7,"az":160,"ss":0,"used":false},'
       '{"PRN":8,"el":66,"az":189,"ss":44,"used":true},'
       '{"PRN":10,"el":51,"az":304,"ss":29,"used":true},'
       '{"PRN":4,"el":15,"az":199,"ss":36,"used":true}]}')

ATT = ('{"class":"ATT","time":"2010-06-04T10:31:00.289Z",'
       '"heading":14223.00,"mag_st":"N",'
       '"pitch":169.00,"pitch_st":"N", "roll":-43.00,"roll_st":"N",'
       '"dip":13641.000,"mag_x":2454.000}')

VERSION = ('{"class":"VERSION","version":"2.40dev",'
           '"rev":"06f62e14eae9886cde907dae61c124c53eb1101f",'
           '"proto_major":16,"proto_minor":1}')


def feed(state, *lines):
    return [state.apply(json.loads(x)) for x in lines]


def test_tpv_fields():
    st = GpsState()
    assert feed(st, TPV) == ["TPV"]
    f = st.fix
    assert f.mode == MODE_3D
    assert math.isclose(f.latitude, 46.498293369)
    assert math.isclose(f.longitude, 7.567411672)
    assert math.isclose(f.speed, 0.091)
    assert math.isclose(f.climb, -0.085)
    assert st.leap_seconds == 18
    assert f.time.tzinfo == timezone.utc
    assert f.time.microsecond == 283000
    # keys absent from the message are NaN, as in libgps
    assert math.isnan(f.alt_hae) and math.isnan(f.magnetic_track)
    assert f.dgps_station == -1


def test_sky_dops_and_satellites():
    st = GpsState()
    feed(st, SKY)
    assert st.dop.hdop == 1.24
    assert math.isnan(st.dop.gdop)
    assert len(st.sats) == 5
    assert st.sats_seen == 5
    assert st.sats_used == 3
    s = st.sats[2]
    assert (s.prn, s.elevation, s.azimuth, s.ss, s.used) == (8, 66.0, 189.0, 44.0, True)


def test_sky_without_satellites_keeps_last_sky_view():
    st = GpsState()
    feed(st, SKY, '{"class":"SKY","hdop":2.0}')
    assert st.dop.hdop == 2.0
    assert len(st.sats) == 5


def test_tpv_without_fields_resets_fix_like_libgps():
    st = GpsState()
    feed(st, TPV, '{"class":"TPV","mode":1}')
    assert st.fix.mode == 1
    assert math.isnan(st.fix.latitude)


def test_attitude_and_version():
    st = GpsState()
    feed(st, ATT, VERSION)
    assert st.attitude.heading == 14223.0
    assert st.attitude.pitch == 169.0
    assert st.attitude.roll == -43.0
    assert st.version.proto_major == 16 and st.version.proto_minor == 1


def test_default_mode_is_not_seen():
    st = GpsState()
    assert st.fix.mode == MODE_NOT_SEEN


def test_unknown_and_bad_input_is_ignored():
    st = GpsState()
    assert st.apply({"class": "DEVICES"}) is None
    assert st.apply({"nope": 1}) is None
    assert feed(st, '{"class":"TPV","lat":"not a number","mode":"3"}') == ["TPV"]
    assert math.isnan(st.fix.latitude)
    assert st.fix.mode == MODE_NOT_SEEN


def test_bad_health_value_is_recognised():
    st = GpsState()
    feed(st, '{"class":"SKY","satellites":[{"PRN":1,"health":2,"used":false}]}')
    assert st.sats[0].health == SAT_HEALTH_BAD


def test_parse_time():
    assert parse_time("2010-09-08T13:33:06.095Z").microsecond == 95000
    assert parse_time(None) is None
    assert parse_time("yesterday") is None
