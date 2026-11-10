"""NMEA decoder: checksums, sentences, epochs, and the gpsd-shaped objects they become."""
import math

from gpsview.model import MODE_2D, MODE_3D, MODE_NO_FIX, GpsState
from gpsview.nmea import NmeaDecoder, checksum, make_sentence, split_sentence

# the sentence from the NMEA 0183 examples; its checksum is 47
GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"


def sent(body: str) -> str:
    """A sentence (without CRLF) with a correct checksum."""
    return f"${body}*{checksum(body):02X}"


def feed_all(dec, *chunks: bytes):
    events = []
    for c in chunks:
        events.extend(dec.feed(c))
    return events


def state_after(*lines: str) -> tuple[GpsState, NmeaDecoder]:
    dec = NmeaDecoder("test")
    st = GpsState()
    for ev in feed_all(dec, *(l.encode() + b"\r\n" for l in lines)):
        if ev.kind == "obj":
            st.apply(ev.obj)
    return st, dec


def test_checksum_matches_the_published_example():
    assert checksum("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,") == 0x47
    assert make_sentence("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,") \
        == (GGA + "\r\n").encode()


def test_split_sentence_checks_and_ignores_non_sentences():
    fields, good = split_sentence(GGA)
    assert good and fields[0] == "GPGGA" and len(fields) == 15   # talker + 14 fields
    assert split_sentence(GGA.replace("*47", "*48"))[1] is False
    assert split_sentence("hello") is None
    fields, good = split_sentence("$GPGGA,1,2")       # no checksum given: accepted
    assert good and fields == ["GPGGA", "1", "2"]


def test_gga_gives_position_altitude_and_fix():
    st, _ = state_after(GGA)
    f = st.fix
    assert f.mode == MODE_3D
    assert math.isclose(f.latitude, 48 + 7.038 / 60)
    assert math.isclose(f.longitude, 11 + 31.0 / 60)
    assert f.alt_msl == 545.4
    assert math.isclose(f.alt_hae, 545.4 + 46.9)          # MSL + geoid separation
    assert (f.time.hour, f.time.minute, f.time.second) == (12, 35, 19)   # no date yet: today's
    assert st.leap_seconds is None                         # NMEA has no leap seconds


def test_rmc_gives_speed_track_and_date():
    st, _ = state_after(RMC)
    f = st.fix
    assert math.isclose(f.speed, 22.4 * 0.514444)           # knots to m/s
    assert f.track == 84.4
    assert math.isclose(f.magnetic_var, -3.1)               # W is negative
    assert f.time.year == 1994 and f.time.month == 3 and f.time.day == 23
    assert f.mode == MODE_2D                                # RMC has no altitude


def test_void_rmc_and_quality_zero_mean_no_fix():
    st, _ = state_after(sent("GPRMC,123519,V,,,,,,,230394,,"))
    assert st.fix.mode == MODE_NO_FIX
    st, _ = state_after(sent("GPGGA,123519,,,,,0,00,99.9,,M,,M,,"))
    assert st.fix.mode == MODE_NO_FIX
    assert math.isnan(st.fix.latitude)


def test_gsa_and_gsv_give_used_satellites_and_dops():
    gsa = "GPGSA,A,3,01,03,07,09,12,14,17,22,,,,,1.2,0.9,1.0"
    gsv1 = "GPGSV,2,1,08,01,60,083,46,03,40,308,41,07,25,344,39,09,70,228,45"
    gsv2 = "GPGSV,2,2,08,12,15,120,40,14,45,010,44,17,35,200,35,22,55,300,42"
    st, _ = state_after(*(sent(s) for s in (gsv1, gsv2, gsa)))
    assert st.sats_seen == 8 and st.sats_used == 8
    assert st.dop.pdop == 1.2 and st.dop.hdop == 0.9 and st.dop.vdop == 1.0
    s = next(s for s in st.sats if s.prn == 7)
    assert (s.elevation, s.azimuth, s.ss, s.used, s.gnssid) == (25.0, 344.0, 39.0, True, 0)


def test_used_satellites_are_the_union_of_every_gsa_in_the_epoch():
    # GNGSA sentences, one per constellation, as a multi-GNSS receiver sends them
    gsa_gps = "GNGSA,A,3,01,03,,,,,,,,,,,1.2,0.9,1.0"
    gsa_gal = "GNGSA,A,3,302,307,,,,,,,,,,,1.2,0.9,1.0"
    gga = "GNGGA,123519,4807.038,N,01131.000,E,1,04,0.9,545.4,M,46.9,M,,"
    gsv = "GPGSV,1,1,02,01,40,083,46,03,17,308,41"
    st, _ = state_after(sent(gga), sent(gsv), sent(gsa_gps), sent(gsa_gal))
    # the Galileo GSA's satellites are not in view here, so only GPS 1 and 3 are counted
    assert sorted(s.prn for s in st.sats if s.used) == [1, 3]


def test_a_new_epoch_clears_the_used_set():
    gsv = "GPGSV,1,1,02,01,40,083,46,03,17,308,41"
    gsa = "GPGSA,A,3,01,03,,,,,,,,,,,1.2,0.9,1.0"
    gga = "GPGGA,123520,4807.038,N,01131.000,E,1,04,0.9,545.4,M,46.9,M,,"
    st, _ = state_after(sent(gsv), sent(gsa), sent(gga), sent(gsv))
    assert st.sats_used == 0


def test_glonass_sentences_use_gnssid_6():
    gsv = "GLGSV,1,1,02,65,30,100,38,66,45,200,41"
    st, _ = state_after(sent(gsv))
    assert [(s.gnssid, s.prn) for s in st.sats] == [(6, 65), (6, 66)]
    assert st.sats_used == 0          # no GSA lists them yet


def test_gst_gives_error_estimates_and_emits_a_tpv():
    gst = "GPGST,123519,1.8,0.0,0.0,0.0,1.6,1.9,3.2"
    dec = NmeaDecoder("test")
    events = dec.feed((sent(gst) + "\r\n").encode())
    assert [e.kind for e in events] == ["raw", "obj"]
    st = GpsState()
    st.apply(events[1].obj)
    assert (st.fix.epx, st.fix.epy, st.fix.epv) == (1.9, 1.6, 3.2)
    assert math.isclose(st.fix.eph, math.hypot(1.6, 1.9))
    assert math.isclose(st.fix.sep, math.hypot(math.hypot(1.6, 1.9), 3.2))


def test_bad_checksum_is_counted_shown_in_red_and_not_used():
    dec = NmeaDecoder("test")
    events = dec.feed((GGA.replace("*47", "*00") + "\r\n").encode())
    assert dec.errors == 1
    assert [e.kind for e in events] == ["raw"] and events[0].style == "red"


def test_sentences_split_across_reads_are_joined():
    data = (GGA + "\r\n" + RMC + "\r\n").encode()
    dec = NmeaDecoder("test")
    events = []
    for i in range(0, len(data), 7):          # odd-sized chunks, as a serial port gives them
        events.extend(dec.feed(data[i:i + 7]))
    raws = [e.text for e in events if e.kind == "raw"]
    assert raws == [GGA, RMC]
    assert dec.errors == 0


def test_a_malformed_field_is_counted_not_fatal():
    bad = "GPGSV,x,y,z"
    dec = NmeaDecoder("test")
    events = dec.feed((sent(bad) + "\r\n").encode())
    assert dec.errors == 1
    assert [e.kind for e in events] == ["raw"]


def test_non_nmea_lines_are_shown_and_ignored():
    dec = NmeaDecoder("test")
    events = dec.feed(b"some banner text\r\n")
    assert [(e.kind, e.text) for e in events] == [("raw", "some banner text")]
    assert dec.errors == 0


def test_epoch_fields_are_replaced_not_accumulated():
    # a later GGA with no fix must not keep the earlier position
    st, _ = state_after(GGA, sent("GPGGA,123520,,,,,0,00,99.9,,M,,M,,"))
    assert st.fix.mode == MODE_NO_FIX
    assert math.isnan(st.fix.latitude)
