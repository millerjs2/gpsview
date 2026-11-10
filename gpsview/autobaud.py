"""Find the baud rate of a serial receiver by listening.

Nothing is sent.  For each candidate rate the port is opened and read for a short time, and
the bytes are checked for whole, valid frames of the protocol (NMEA sentences with a good
checksum, UBX frames with a good Fletcher checksum, SBP frames with a good CRC-16).  Bytes read
at the wrong rate are noise, and noise does not pass those checks, so two valid frames are
strong evidence.  The first rate that gets there wins.

It cannot work if the receiver sends nothing; the result says whether bytes were seen at all.
"""
from __future__ import annotations

import asyncio
import binascii
import re
from dataclasses import dataclass, field
from typing import Callable

from .transports import DEFAULT_BAUD, SerialTransport, Transport
from .ubx import CK_LEN as UBX_CK, CLASSES as UBX_CLASSES, HEADER as UBX_HEADER
from .ubx import MAX_PAYLOAD as UBX_MAX, fletcher

COMMON = (115200, 38400, 9600, 57600, 19200, 4800, 230400, 460800, 921600)
DWELL = 2.0               # seconds to listen at each rate
NEED = 2                  # valid frames that settle it
KEEP = 1 << 16            # bytes of unparsed input kept while scoring

_NMEA = re.compile(rb"[$!][A-Z0-9]{2,8}[ -~]*\*[0-9A-Fa-f]{2}")


def candidate_bauds(protocol: str, preferred: int | None = None) -> list[int]:
    """The rates to try, most likely first: a given rate, the protocol's default, then the
    common ones."""
    order = [preferred, DEFAULT_BAUD.get(protocol), *COMMON]
    seen: list[int] = []
    for b in order:
        if b and b not in seen:
            seen.append(b)
    return seen


class Scorer:
    """Counts valid frames in a byte stream, fed in pieces."""

    def __init__(self, protocol: str) -> None:
        if protocol not in ("nmea", "ubx", "sbp"):
            raise ValueError(f"cannot detect the baud rate of '{protocol}'")
        self.protocol = protocol
        self.frames = 0
        self._buf = bytearray()

    def feed(self, data: bytes) -> int:
        self._buf += data
        {"nmea": self._nmea, "ubx": self._ubx, "sbp": self._sbp}[self.protocol]()
        if len(self._buf) > KEEP:
            del self._buf[:-KEEP]
        return self.frames

    def _nmea(self) -> None:
        *lines, rest = bytes(self._buf).split(b"\n")
        for line in lines:
            m = _NMEA.search(line)
            if m:
                text = m.group()
                body, _, given = text[1:].rpartition(b"*")
                check = 0
                for c in body:
                    check ^= c
                if int(given, 16) == check:
                    self.frames += 1
        self._buf[:] = rest

    def _ubx(self) -> None:
        buf = self._buf
        while True:
            i = buf.find(b"\xb5\x62")
            if i < 0:
                del buf[:max(0, len(buf) - 1)]            # keep a possible lone B5
                return
            del buf[:i]
            if len(buf) < UBX_HEADER:
                return
            length = buf[4] | buf[5] << 8
            if buf[2] not in UBX_CLASSES or length > UBX_MAX:
                del buf[:1]
                continue
            total = UBX_HEADER + length + UBX_CK
            if len(buf) < total:
                return
            if fletcher(buf[2:UBX_HEADER + length]) == (buf[total - 2], buf[total - 1]):
                self.frames += 1
                del buf[:total]
            else:
                del buf[:1]

    def _sbp(self) -> None:
        buf = self._buf
        while True:
            i = buf.find(0x55)
            if i < 0:
                buf.clear()
                return
            del buf[:i]
            if len(buf) < 6:
                return
            total = 6 + buf[5] + 2
            if len(buf) < total:
                return
            crc = int.from_bytes(buf[total - 2:total], "little")
            if binascii.crc_hqx(bytes(buf[1:total - 2]), 0) == crc:
                self.frames += 1
                del buf[:total]
            else:
                del buf[:1]


@dataclass
class Attempt:
    baud: int
    bytes: int
    frames: int


@dataclass
class Detection:
    baud: int | None = None
    attempts: list[Attempt] = field(default_factory=list)
    error: str = ""                      # the port could not be opened

    @property
    def saw_bytes(self) -> bool:
        return any(a.bytes for a in self.attempts)

    def summary(self) -> str:
        """For the user when nothing was found."""
        if self.error:
            return self.error
        rates = ", ".join(str(a.baud) for a in self.attempts)
        if not self.saw_bytes:
            return (f"No data at any of {rates} baud. Is the receiver powered and sending "
                    "on this port?")
        return (f"Data arrived but no valid frames at any of {rates} baud. Is the protocol "
                "right? A receiver set to another protocol, or a rate not on the list, looks "
                "like this.")


async def detect_baud(path: str, protocol: str, *, preferred: int | None = None,
                      candidates: list[int] | None = None, dwell: float = DWELL,
                      need: int = NEED,
                      make_transport: Callable[[str, int], Transport] = SerialTransport,
                      progress: Callable[[int], None] | None = None) -> Detection:
    """Listen at each candidate rate until `need` valid frames arrive.  Never writes."""
    result = Detection()
    loop = asyncio.get_running_loop()
    for baud in candidates or candidate_bauds(protocol, preferred):
        if progress is not None:
            progress(baud)
        transport = make_transport(path, baud)
        scorer = Scorer(protocol)
        seen = 0
        try:
            await transport.open()
            end = loop.time() + dwell
            while scorer.frames < need and (left := end - loop.time()) > 0:
                data = await transport.read(min(0.25, left))
                if data:
                    seen += len(data)
                    scorer.feed(data)
        except OSError as exc:
            result.error = f"Cannot open {path}: {exc}"
            return result
        finally:
            await transport.close()
        result.attempts.append(Attempt(baud, seen, scorer.frames))
        if scorer.frames >= need:
            result.baud = baud
            return result
    return result
