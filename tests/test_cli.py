"""Command line: one source at a time, and the -n / -sbp / -ubx forms."""
import pytest

from gpsview.__main__ import build_parser, main


def test_sbp_needs_a_source_and_keeps_silent_separate():
    args = build_parser().parse_args(["-sbp", "/dev/ttyUSB0:115200", "-s"])
    assert args.sbp == "/dev/ttyUSB0:115200"
    assert args.silent is True
    with pytest.raises(SystemExit):                  # no default receiver address
        build_parser().parse_args(["-s", "-sbp"])


def test_nmea_flag_takes_a_source():
    args = build_parser().parse_args(["-n", "/dev/ttyACM0"])
    assert args.nmea == "/dev/ttyACM0" and args.server is None


def test_ubx_takes_a_source_and_has_no_default():
    args = build_parser().parse_args(["-ubx", "/dev/ttyACM0:9600", "--file-rate", "0"])
    assert args.ubx == "/dev/ttyACM0:9600" and args.file_rate == 0
    with pytest.raises(SystemExit):
        build_parser().parse_args(["-ubx"])


def test_ubx_does_not_swallow_the_units_option():
    args = build_parser().parse_args(["-ubx", "tcp://h:1", "-u", "m"])
    assert args.ubx == "tcp://h:1" and args.units == "m"


@pytest.mark.parametrize("argv, message", [
    (["-ubx", "udp://:1", "-n", "/dev/ttyUSB0"], "choose one source"),
    (["-ubx", "pi.local"], "needs a port"),
    (["-ubx", "file:///no/such/file"], "no such file"),
    (["-ubx", "tcp://h:1", "--file-rate", "-5"], "--file-rate"),
])
def test_ubx_refusals(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [
    (["-n", "/dev/ttyUSB0", "pi.local"], "choose one source"),
    (["-n", "/dev/ttyUSB0", "-sbp", "192.0.2.10:1"], "choose one source"),
    (["-sbp", "192.0.2.10:55555", "pi.local"], "choose one source"),
    (["-n", "10.0.0.5"], "needs a port"),
    (["-sbp", "http://x:1"], "unknown scheme"),
    (["-n", "udp://[::1"], "missing ']'"),
])
def test_conflicting_or_bad_sources_are_refused_before_the_app_starts(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)                       # returns only if it would start the app
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
