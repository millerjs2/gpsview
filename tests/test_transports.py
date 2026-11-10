"""Transports: endpoint grammar, TCP, UDP and a pty standing in for a serial tty."""
import asyncio
import os
import socket

import pytest

from gpsview.transports import (EndOfFile, Endpoint, FileTransport, SerialTransport,
                                TcpTransport, UdpTransport, parse_endpoint)


def free_port(kind=socket.SOCK_STREAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- grammar
def test_serial_paths_and_default_baud():
    assert parse_endpoint("/dev/ttyUSB0", "nmea") == Endpoint("serial", path="/dev/ttyUSB0", baud=9600)
    assert parse_endpoint("/dev/ttyUSB0", "sbp") == Endpoint("serial", path="/dev/ttyUSB0", baud=115200)
    assert parse_endpoint("/dev/ttyUSB0:4800", "nmea") == Endpoint("serial", path="/dev/ttyUSB0", baud=4800)


def test_tcp_and_udp_forms():
    assert parse_endpoint("10.0.0.5:2000", "nmea") == Endpoint("tcp", host="10.0.0.5", port=2000)
    assert parse_endpoint("tcp://10.0.0.5:2000", "nmea") == Endpoint("tcp", host="10.0.0.5", port=2000)
    assert parse_endpoint("udp://:3000", "sbp") == Endpoint("udp", host="", port=3000)
    assert parse_endpoint("udp://[::1]:3000", "sbp") == Endpoint("udp", host="::1", port=3000)
    assert parse_endpoint("[fe80::1]:2000", "nmea") == Endpoint("tcp", host="fe80::1", port=2000)


@pytest.mark.parametrize("spec", ["", "10.0.0.5", "tcp://:2000", "http://x:80",
                                  "::1:2000", "host:0", "host:70000", "host:abc"])
def test_bad_specs_are_refused(spec):
    with pytest.raises(ValueError):
        parse_endpoint(spec, "nmea")


def test_labels_show_the_scheme():
    assert parse_endpoint("192.0.2.10:55555", "sbp").label == "tcp://192.0.2.10:55555"
    assert parse_endpoint("udp://[::1]:9", "sbp").label == "udp://[::1]:9"
    assert parse_endpoint("/dev/ttyACM0", "nmea").label == "/dev/ttyACM0"


# ----------------------------------------------------------------- UDP
def test_udp_receives_datagrams_and_times_out():
    async def scenario():
        port = free_port(socket.SOCK_DGRAM)
        t = UdpTransport("127.0.0.1", port)
        await t.open()
        try:
            assert await t.read(0.05) is None            # nothing yet: a timeout, not an error
            with pytest.raises(OSError, match="no datagram has arrived"):
                await t.write(b"\xd3")                    # nowhere to send to yet
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.bind(("127.0.0.1", 0))
            sender.sendto(b"$GPGGA,1*00\r\n", ("127.0.0.1", port))
            assert await t.read(2.0) == b"$GPGGA,1*00\r\n"
            assert t.peer == sender.getsockname()
            await t.write(b"\xd3\x00\x00")                 # replies to the sender's address
            sender.settimeout(2.0)
            assert sender.recvfrom(64)[0] == b"\xd3\x00\x00"
            sender.close()
        finally:
            await t.close()
    run(scenario())


def test_udp_out_sends_to_a_fixed_address():
    from gpsview.transports import UdpOutTransport

    async def scenario():
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2.0)
        t = UdpOutTransport("127.0.0.1", rx.getsockname()[1])
        await t.open()
        await t.write(b"hello")
        assert rx.recvfrom(64)[0] == b"hello"
        await t.close()
        with pytest.raises(OSError):
            await t.write(b"x")
        rx.close()
    run(scenario())


# ----------------------------------------------------------------- TCP
def test_tcp_reads_then_reports_the_peer_closing():
    async def scenario():
        async def handle(reader, writer):
            writer.write(b"hello")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        t = TcpTransport("127.0.0.1", port)
        await t.open()
        try:
            assert await t.read(2.0) == b"hello"
            with pytest.raises(ConnectionError):
                await t.read(2.0)
        finally:
            await t.close()
            server.close()
            await server.wait_closed()
    run(scenario())


def test_tcp_refused_connection_is_an_oserror():
    async def scenario():
        t = TcpTransport("127.0.0.1", free_port())
        with pytest.raises(OSError):
            await t.open()
    run(scenario())


# ----------------------------------------------------------------- serial (pty)
def test_serial_reads_from_a_pty():
    master, slave = os.openpty()
    try:
        async def scenario():
            t = SerialTransport(os.ttyname(slave), 9600)
            await t.open()
            try:
                assert await t.read(0.1) is None
                os.write(master, b"$GPRMC,1*00\r\n")
                got = b""
                for _ in range(20):
                    chunk = await t.read(0.2)
                    if chunk:
                        got += chunk
                    if got.endswith(b"\n"):
                        break
                assert got == b"$GPRMC,1*00\r\n"
            finally:
                await t.close()
        run(scenario())
    finally:
        os.close(master)
        os.close(slave)


def test_serial_open_failure_is_an_oserror():
    async def scenario():
        t = SerialTransport("/dev/this-tty-does-not-exist", 9600)
        with pytest.raises(OSError):
            await t.open()
    run(scenario())


# ----------------------------------------------------------------- files
def test_file_endpoints(tmp_path, monkeypatch):
    f = tmp_path / "capture.ubx"
    f.write_bytes(b"abc")
    want = Endpoint("file", path=str(f))
    assert parse_endpoint(f"file://{f}", "ubx") == want
    assert parse_endpoint(str(f), "ubx") == want                  # a regular file, not a tty
    assert want.label == f"file://{f}"
    assert want.device == ""
    monkeypatch.chdir(tmp_path)
    assert parse_endpoint("./capture.ubx", "sbp") == Endpoint("file", path="./capture.ubx")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert parse_endpoint("~/capture.ubx", "nmea") == want


def test_a_tty_path_is_still_serial_and_ubx_has_a_default_baud():
    assert parse_endpoint("/dev/ttyACM0", "ubx") == Endpoint("serial", path="/dev/ttyACM0", baud=38400)
    assert parse_endpoint("/dev/ttyACM0:9600", "ubx").baud == 9600


@pytest.mark.parametrize("spec, message", [
    ("file:///no/such/file", "no such file"),
    ("file://", "needs a path"),
    ("file:///dev/null", "not a regular file"),
    ("./no-such-file", "no such file"),
])
def test_bad_file_specs(spec, message):
    with pytest.raises(ValueError, match=message):
        parse_endpoint(spec, "ubx")


def test_file_transport_replays_paced_then_raises_end_of_file(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(bytes(range(256)) * 4)                          # 1024 bytes

    async def scenario():
        t = FileTransport(str(f), rate=2000)                      # 100 bytes per 50 ms tick
        await t.open()
        got = b""
        chunks = 0
        start = asyncio.get_running_loop().time()
        with pytest.raises(EndOfFile) as exc:
            while True:
                got += await t.read(1.0)
                chunks += 1
        elapsed = asyncio.get_running_loop().time() - start
        await t.close()
        assert got == f.read_bytes()
        assert chunks == 11 and elapsed >= 0.4                    # paced, not all at once
        assert exc.value.size == 1024 and isinstance(exc.value, ConnectionError)
        with pytest.raises(OSError):
            await t.write(b"x")                                   # a file is never written
    run(scenario())


def test_file_transport_unpaced(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"z" * 100000)

    async def scenario():
        t = FileTransport(str(f), rate=0)
        await t.open()
        assert len(await t.read(1.0)) == 65536
        assert len(await t.read(1.0)) == 100000 - 65536
        with pytest.raises(EndOfFile):
            await t.read(1.0)
        await t.close()
    run(scenario())


def test_serial_transport_writes_to_the_tty():
    import pty
    master, slave = pty.openpty()
    tty = os.ttyname(slave)

    async def scenario():
        t = SerialTransport(tty, 115200)
        await t.open()
        await t.write(b"\xd3\x00\x01hello")
        await t.close()
    try:
        run(scenario())
        os.set_blocking(master, False)
        assert os.read(master, 64) == b"\xd3\x00\x01hello"
    finally:
        os.close(master)
        os.close(slave)
