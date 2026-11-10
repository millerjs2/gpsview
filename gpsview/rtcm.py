"""RTCM 3 framing: find whole frames in a byte stream, check their CRC-24Q, name their type.

Frame: 0xD3, 6 reserved bits and a 10-bit length, the payload, then a CRC-24Q of everything
before it (3 bytes, big-endian).  The first 12 bits of the payload are the message type.

This module does not decode messages.  gpsview forwards RTCM to a receiver untouched; it
only needs the frame boundaries (so a datagram holds whole frames, and a bad stretch is
dropped rather than sent on) and the type numbers (for the status line and the log).
"""
from __future__ import annotations

from dataclasses import dataclass

PREAMBLE = 0xD3
HEADER = 3
CRC_LEN = 3
MAX_PAYLOAD = 1023
MAX_FRAME = HEADER + MAX_PAYLOAD + CRC_LEN


def _make_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
        table.append(crc & 0xFFFFFF)
    return table


_TABLE = _make_table()


def crc24q(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _TABLE[(crc >> 16) ^ b]
    return crc


def make_frame(payload: bytes) -> bytes:
    """A frame around `payload` (used by tests and the fake caster)."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("RTCM payload is at most 1023 bytes")
    body = bytes([PREAMBLE, len(payload) >> 8, len(payload) & 0xFF]) + payload
    return body + crc24q(body).to_bytes(3, "big")


def message_type(frame: bytes) -> int:
    """The message number of a whole frame, or 0 if the payload is too short to have one."""
    if len(frame) < HEADER + 2 + CRC_LEN:
        return 0
    return (frame[3] << 4) | (frame[4] >> 4)


def check(buf: bytes | bytearray, start: int = 0) -> tuple[str, int]:
    """Look at an RTCM frame starting at buf[start] (which must be 0xD3).

    Returns ("ok", total) for a whole frame with a good CRC, ("need", total) when more
    bytes are needed, or ("bad", 0) when it is not a frame (reserved bits set, or bad CRC).
    """
    have = len(buf) - start
    if have < HEADER:
        return "need", HEADER
    if buf[start + 1] & 0xFC:                  # reserved bits are zero in a real frame
        return "bad", 0
    total = HEADER + (((buf[start + 1] & 0x03) << 8) | buf[start + 2]) + CRC_LEN
    if have < total:
        return "need", total
    frame = bytes(buf[start:start + total])
    if crc24q(frame[:-CRC_LEN]) != int.from_bytes(frame[-CRC_LEN:], "big"):
        return "bad", 0
    return "ok", total


@dataclass(frozen=True)
class RtcmFrame:
    type: int
    data: bytes               # the whole frame, as it arrived


class RtcmFramer:
    """Splits a stream into whole, CRC-checked frames.  Bytes that are not part of a good
    frame are dropped and counted in `skipped`; the framer resynchronises on the next 0xD3."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.skipped = 0
        self.bad_crc = 0

    def feed(self, data: bytes) -> list[RtcmFrame]:
        self._buf += data
        frames: list[RtcmFrame] = []
        buf = self._buf
        while buf:
            if buf[0] != PREAMBLE:
                start = buf.find(PREAMBLE)
                n = len(buf) if start < 0 else start
                self.skipped += n
                del buf[:n]
                continue
            state, total = check(buf)
            if state == "need":
                break
            if state == "bad":
                self.bad_crc += 1
                self.skipped += 1
                del buf[:1]
                continue
            frame = bytes(buf[:total])
            del buf[:total]
            frames.append(RtcmFrame(message_type(frame), frame))
        return frames
