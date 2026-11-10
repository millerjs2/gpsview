"""Transports: endpoint grammar, TCP, UDP and a pty standing in for a serial tty."""
import asyncio
import os
import socket

import pytest

from gpsview.transports import (Endpoint, SerialTransport, TcpTransport, UdpTransport,
                                parse_endpoint)


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
    assert parse_endpoint("172.16.0.120:55555", "sbp").label == "tcp://172.16.0.120:55555"
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
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b"$GPGGA,1*00\r\n", ("127.0.0.1", port))
            sender.close()
            assert await t.read(2.0) == b"$GPGGA,1*00\r\n"
            with pytest.raises(OSError):
                await t.write(b"?WATCH")                  # read-only
        finally:
            await t.close()
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
