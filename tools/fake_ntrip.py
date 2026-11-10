"""A fake NTRIP caster, for demos and tests.  No network or account needed.

    python3 tools/fake_ntrip.py --port 2101                     # mountpoints RTCM and VRS
    python3 tools/fake_ntrip.py --port 2101 --user me --password secret --version 1 --chunked
    bin/gpsview -ubx tcp://127.0.0.1:10113 --ntrip ntrip://me:secret@127.0.0.1:2101/RTCM

It serves RTCM 3 frames (types 1005, 1077, 1087, 1230, with valid CRCs) on mountpoint RTCM, and
the same on VRS only after the client has sent a GGA sentence.  Anything else gets the
sourcetable, as a real caster does.  How the reply looks is selectable:

    style "v1"           ICY 200 OK, then raw data
    style "v1-bare"      ICY 200 OK with no blank line after it, then data at once
    style "v2"           HTTP/1.1 200 OK, raw data, Connection: close
    style "v2-chunked"   HTTP/1.1 200 OK with Transfer-Encoding: chunked (frames are split
                         across chunk boundaries, to catch a client that does not de-chunk)

The caster records what it was sent (`requests`, `gga`) so tests can check it.
"""
from __future__ import annotations

import os as _os
import sys as _sys

# Run as  python tools/fake_x.py : the project root (where the gpsview package is) is not on
# sys.path then, only this directory.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import asyncio
import base64

from gpsview import rtcm

SOURCETABLE = (
    "STR;RTCM;Fake Base;RTCM 3.3;1005(10),1077(1),1087(1),1230(30);2;GPS+GLO;FAKE;USA;"
    "37.77;-122.42;0;1;gpsview;none;B;N;9600;\r\n"
    "STR;VRS;Fake VRS;RTCM 3.3;1005(10),1077(1);2;GPS+GLO;FAKE;USA;37.77;-122.42;1;1;"
    "gpsview;none;B;N;9600;\r\n"
    "ENDSOURCETABLE\r\n")


def frames_for(seq: int) -> list[bytes]:
    """One epoch of frames.  Payloads carry the sequence number so a test can see the order."""
    out = []
    for msg_type in (1005, 1077, 1087, 1230):
        payload = bytes([msg_type >> 4, (msg_type & 0xF) << 4]) + seq.to_bytes(2, "big") + bytes(
            (seq + i) & 0xFF for i in range(40))
        out.append(rtcm.make_frame(payload))
    return out


class FakeCaster:
    def __init__(self, *, style: str = "v2", user: str = "", password: str = "",
                 rate: float = 0.1, drop_after: int = 0, sourcetable: str = SOURCETABLE) -> None:
        self.style, self.user, self.password = style, user, password
        self.sourcetable = sourcetable
        self.rate = rate
        self.drop_after = drop_after            # close the stream after this many epochs (0 = never)
        self.requests: list[dict] = []
        self.gga: list[bytes] = []
        self.sent = b""                         # every payload byte sent, in order (de-chunked)
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        self.connections += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {k.strip().lower(): v.strip()
                       for k, _, v in (ln.partition(":") for ln in lines[1:]) if v}
            method, path, version = lines[0].split(" ")
            mount = path.lstrip("/")
            self.requests.append({"line": lines[0], "mount": mount, "headers": headers})
            if self.user and headers.get("authorization") != "Basic " + base64.b64encode(
                    f"{self.user}:{self.password}".encode()).decode():
                writer.write(b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=\"NTRIP\""
                             b"\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            if mount not in ("RTCM", "VRS"):                       # sourcetable
                body = self.sourcetable.encode()
                if version == "HTTP/1.0":
                    writer.write(b"SOURCETABLE 200 OK\r\nContent-Type: text/plain\r\n"
                                 b"Content-Length: %d\r\n\r\n" % len(body) + body)
                else:
                    writer.write(b"HTTP/1.1 200 OK\r\nNtrip-Version: Ntrip/2.0\r\nContent-Type: "
                                 b"gnss/sourcetable\r\nContent-Length: %d\r\n\r\n" % len(body)
                                 + body)
                await writer.drain()
                return
            if self.style == "v1":
                writer.write(b"ICY 200 OK\r\n\r\n")
            elif self.style == "v1-bare":
                writer.write(b"ICY 200 OK\r\n")
            elif self.style == "v2-chunked":
                writer.write(b"HTTP/1.1 200 OK\r\nNtrip-Version: Ntrip/2.0\r\nContent-Type: "
                             b"gnss/data\r\nTransfer-Encoding: chunked\r\n\r\n")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nNtrip-Version: Ntrip/2.0\r\nContent-Type: "
                             b"gnss/data\r\n\r\n")
            await writer.drain()
            listener = asyncio.create_task(self._listen(reader))
            try:
                if mount == "VRS":                                   # silent until we have a GGA
                    while not self.gga:
                        await asyncio.sleep(0.02)
                seq = 0
                while not self.drop_after or seq < self.drop_after:
                    data = b"".join(frames_for(seq))
                    self.sent += data
                    writer.write(self._wrap(data))
                    await writer.drain()
                    seq += 1
                    await asyncio.sleep(self.rate)
                if self.style == "v2-chunked":
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()
            finally:
                listener.cancel()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()

    def _wrap(self, data: bytes) -> bytes:
        if self.style != "v2-chunked":
            return data
        # odd chunk sizes, so frames straddle chunk boundaries
        out, i, size = b"", 0, 37
        while i < len(data):
            part = data[i:i + size]
            out += b"%x\r\n" % len(part) + part + b"\r\n"
            i += size
            size = 101 if size == 37 else 37
        return out

    async def _listen(self, reader: asyncio.StreamReader) -> None:
        """What the client sends after its request: GGA sentences, one per line."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                self.gga.append(line)
        except (ConnectionError, asyncio.CancelledError):
            pass


async def _main(args: argparse.Namespace) -> None:
    caster = FakeCaster(style=("v2-chunked" if args.chunked else f"v{args.version}"),
                        user=args.user, password=args.password, rate=args.rate)
    port = await caster.start(args.host, args.port)
    print(f"fake NTRIP caster on {args.host}:{port}, mountpoints RTCM and VRS (Ctrl-C to stop)",
          flush=True)
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser(description="a fake NTRIP caster for gpsview")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2101)
    ap.add_argument("--version", type=int, choices=[1, 2], default=2)
    ap.add_argument("--chunked", action="store_true", help="version 2 with chunked encoding")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--rate", type=float, default=1.0, help="seconds between epochs")
    args = ap.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
