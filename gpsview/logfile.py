"""The raw data log: every byte the source sends, written as it arrives.

File name:  gpsview_<protocol>_yyyymmdd_hhmmZ.log   (UTC; protocol gpsd, nmea or sbp)
The file holds the bytes exactly as received, so binary SBP frames are kept intact and can
be decoded again later.  If a log with the same name already exists (two sessions started
in the same minute), a _2, _3 ... suffix is added; an existing file is never overwritten.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone


def log_name(protocol: str, now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"gpsview_{protocol}_{now:%Y%m%d_%H%M}Z.log"


class RawLog:
    def __init__(self, directory: str, protocol: str, now: datetime | None = None) -> None:
        name = log_name(protocol, now)
        stem, ext = name[:-len(".log")], ".log"
        self.path = os.path.join(directory, name)
        number = 1
        while True:
            try:
                self._fh = open(self.path, "xb")       # exclusive: never overwrite
                break
            except FileExistsError:
                number += 1
                self.path = os.path.join(directory, f"{stem}_{number}{ext}")
        self.bytes_written = 0

    def write(self, data: bytes) -> None:
        self._fh.write(data)
        self._fh.flush()                               # a crash loses at most the last read
        self.bytes_written += len(data)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
