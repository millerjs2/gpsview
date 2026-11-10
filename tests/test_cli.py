"""Command line: one source at a time, and the -n / -sbp forms."""
import pytest

from gpsview.__main__ import build_parser, main
from gpsview.swift import SBP_DEFAULT


def test_sbp_without_a_source_uses_the_default_endpoint():
    args = build_parser().parse_args(["-sbp"])
    assert args.sbp == SBP_DEFAULT == "tcp://172.16.0.120:55555"


def test_sbp_takes_an_optional_source_and_keeps_silent_separate():
    args = build_parser().parse_args(["-sbp", "/dev/ttyUSB0:115200", "-s"])
    assert args.sbp == "/dev/ttyUSB0:115200"
    assert args.silent is True
    args = build_parser().parse_args(["-s", "-sbp"])
    assert args.sbp == SBP_DEFAULT and args.silent is True


def test_nmea_flag_takes_a_source():
    args = build_parser().parse_args(["-n", "/dev/ttyACM0"])
    assert args.nmea == "/dev/ttyACM0" and args.server is None


@pytest.mark.parametrize("argv, message", [
    (["-n", "/dev/ttyUSB0", "pi.local"], "choose one source"),
    (["-n", "/dev/ttyUSB0", "-sbp"], "choose one source"),
    (["-sbp", "172.16.0.120:55555", "pi.local"], "choose one source"),
    (["-n", "10.0.0.5"], "needs a port"),
    (["-sbp", "http://x:1"], "unknown scheme"),
    (["-n", "udp://[::1"], "missing ']'"),
])
def test_conflicting_or_bad_sources_are_refused_before_the_app_starts(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)                       # returns only if it would start the app
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
