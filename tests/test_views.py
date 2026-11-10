"""Panel text, checked against the layout cgps prints."""
import math
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

from gpsview import views
from gpsview.model import GpsState
from gpsview.views import UNITS, ViewOpts, StateTimer

NOW = datetime(2026, 11, 10, 12, 0, 0, 500000, tzinfo=timezone.utc)

FIX = {"class": "TPV", "time": "2026-11-10T12:00:00.000Z", "mode": 3, "status": 1,
       "lat": 37.7749, "lon": -122.4194, "altHAE": 10.0, "altMSL": 12.0,
       "speed": 2.0, "track": 90.0, "magvar": 13.1, "magtrack": 77.0,
       "climb": 0.5, "epx": 1.5, "epy": 1.5, "epv": 3.0, "eph": 2.0,
       "sep": 4.0, "epd": 2.0, "eps": 0.1, "leapseconds": 18,
       "ecefx": -2.7e6, "ecefy": -4.2e6, "ecefz": 3.8e6,
       "ecefvx": 0.1, "ecefvy": -0.2, "ecefvz": 0.05}


def state_with(*objs):
    st = GpsState()
    for o in objs:
        st.apply(o)
    return st


def gnss(st, opts=ViewOpts(), online=True, secs=5):
    return views.gnss_lines(st, opts, online=online, secs=secs, now=NOW)


def test_gnss_lines_fix_values_and_units():
    lines = gnss(state_with(FIX))
    text = "\n".join(lines)
    assert "Time         2026-11-10T12:00:00.000Z (18)" in text
    assert "Latitude         " in text and "37.77490000 N" in text
    assert "Longitude        122.41940000 W" in text
    # imperial: 10 m -> 32.808 ft
    assert "32.808" in text
    assert "mph" in text
    assert "Status" in text and "3D FIX (5 secs)" in text
    assert "Grid Square" in text and "CM87" in text


def test_metric_units_and_degree_format():
    lines = gnss(state_with(FIX), ViewOpts(units=UNITS["m"], deg="ddmm"))
    text = "\n".join(lines)
    assert "km/h" in text
    assert "10.000" in text            # altitude in metres
    assert "37 46.494000'" in text     # DD MM.mmmmmm


def test_no_fix_and_offline_show_na_and_offline():
    st = GpsState()
    text = "\n".join(gnss(st, online=False, secs=0))
    assert "OFFLINE" in text
    assert "Latitude         n/a" in text
    text = "\n".join(gnss(st, online=True, secs=12))
    assert "NO FIX (12 secs)" in text


def test_magnetic_track():
    text = "\n".join(gnss(state_with(FIX), ViewOpts(magnetic=True)))
    assert "(mag, var)" in text
    assert "77.0" in text
    text = "\n".join(gnss(state_with(FIX)))
    assert "(true, var)" in text and "90.0" in text


def test_time_offset_and_sign():
    fix_time = NOW - timedelta(milliseconds=250)
    assert views.time_offset(fix_time, NOW).startswith(" 0.250000000")
    assert views.time_offset(NOW + timedelta(seconds=1), NOW).startswith("-1.000000000")
    far = NOW - timedelta(days=2)
    assert views.time_offset(far, NOW).strip().endswith("s")


def test_sat_rows_order_and_codes():
    st = state_with({"class": "SKY", "satellites": [
        {"PRN": 23, "el": 6, "az": 84, "ss": 0, "used": False, "gnssid": 0, "svid": 23, "sigid": 0},
        {"PRN": 8, "el": 66, "az": 189, "ss": 44, "used": True, "gnssid": 0, "svid": 8, "sigid": 0},
        {"PRN": 1, "el": 40, "az": 10, "ss": 30, "used": True, "gnssid": 6, "svid": 1, "sigid": 2,
         "health": 2},
    ]})
    rows = views.sat_rows(st.sats)
    assert [r[2] for r in rows] == [want for want in ("  1", "  8", " 23")]
    assert rows[0][0] == "GL" and rows[0][1] == "2" and rows[0][6] == "uY"
    assert rows[1][6] == " Y" and rows[2][6] == " N"
    assert rows[2][3] == "  6.0"       # elevation, %5.1f


def test_sigid_characters():
    assert views._sigid_char(2) == "2"
    assert views._sigid_char(10) == "A"
    assert views._sigid_char(12) == "C"
    assert views._sigid_char(1) == " "


def test_imu_and_rtk_boxes():
    st = state_with({"class": "ATT", "heading": 45.5, "pitch": 1.25, "roll": -2.0,
                     "baseS": 3, "baseE": 0.5, "baseN": -1.0, "time": "2026-11-10T12:00:00Z"},
                    {"class": "IMU", "msg": "UBX-ESF-RAW", "acc_x": 0.1, "gyro_x": 2.5,
                     "timeTag": 42})
    att = "\n".join(views.imu_lines(st.attitude, attitude=True))
    assert "True Heading" in att and "45.5000" in att
    imu = "\n".join(views.imu_lines(st.imu, attitude=False))
    assert "UBX-ESF-RAW" in imu and "timeTag" in imu and "42" in imu
    assert views.imu_lines(None, attitude=True) == ["  no data yet"]
    rtk = "\n".join(views.rtk_att_lines(st.attitude))
    assert "RTKFIX" in rtk and "East" in rtk
    tpv = "\n".join(views.rtk_tpv_lines(state_with({"class": "TPV", "mode": 3, "baseS": 3,
                                                   "dgpsSta": 1022, "dgpsRatio": 4.5}).fix))
    assert "RTKFIX" in tpv and "1022" in tpv and "4.5000" in tpv


def test_status_lines():
    st = state_with(FIX, {"class": "VERSION", "release": "3.27.6", "proto_major": 16,
                          "proto_minor": 1})
    lines = views.status_lines(st, ViewOpts(), link="localhost:2947", online=True, secs=7,
                               rx_bytes=2048, raw_on=True)
    assert "gpsd 3.27.6" in lines[0] and "localhost:2947" in lines[0]
    assert "3D FIX (7 secs)" in lines[1] and "rx 2048 B" in lines[1]
    assert "raw on" in lines[2]


def test_state_timer():
    t = StateTimer()
    assert t.seconds(3, now=100.0) == 0
    assert t.seconds(3, now=104.9) == 4
    assert t.seconds(1, now=105.0) == 0   # state changed: restart


def test_default_units_from_environment():
    with mock.patch.dict(os.environ, {"GPSD_UNITS": "metric"}, clear=True):
        assert views.default_units() == "m"
    with mock.patch.dict(os.environ, {"LANG": "de_DE.UTF-8"}, clear=True):
        assert views.default_units() == "m"
    with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
        assert views.default_units() == "i"
    with mock.patch.dict(os.environ, {}, clear=True):
        assert views.default_units() == "i"


def test_dops_show_when_nan():
    lines = gnss(GpsState())
    assert any("Long Err" in l and "n/a" in l for l in lines)
    assert math.isnan(GpsState().dop.xdop)
