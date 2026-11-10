import json

from gpsview.client import parse_source, watch_command


def test_default_source():
    s = parse_source(None)
    assert (s.host, s.port, s.device) == ("localhost", "2947", None)
    assert s.label == "localhost:2947"


def test_server_port_device_forms():
    assert parse_source("pi.local") == parse_source("pi.local")
    s = parse_source("pi.local")
    assert (s.host, s.port, s.device) == ("pi.local", "2947", None)
    s = parse_source("pi.local:2950")
    assert (s.host, s.port, s.device) == ("pi.local", "2950", None)
    s = parse_source("pi.local:2950:/dev/ttyACM0")
    assert (s.host, s.port, s.device) == ("pi.local", "2950", "/dev/ttyACM0")
    s = parse_source("pi.local::/dev/ttyACM0")
    assert (s.port, s.device) == ("2947", "/dev/ttyACM0")


def test_bare_device_and_ipv6():
    s = parse_source("/dev/ttyUSB0")
    assert (s.host, s.port, s.device) == ("localhost", "2947", "/dev/ttyUSB0")
    s = parse_source("[::1]:2950")
    assert (s.host, s.port, s.device) == ("::1", "2950", None)
    s = parse_source("[fe80::1]:2950:/dev/ttyS0")
    assert (s.host, s.port, s.device) == ("fe80::1", "2950", "/dev/ttyS0")


def test_watch_command():
    line = watch_command(None)
    assert line.endswith(b"\n")
    assert json.loads(line[len(b"?WATCH="):]) == {"enable": True, "json": True}
    line = watch_command("/dev/ttyACM0")
    assert json.loads(line[len(b"?WATCH="):])["device"] == "/dev/ttyACM0"
