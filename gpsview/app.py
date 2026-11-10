"""gpsview: a Textual clone of cgps.  Layout and dialogs follow the tuiStyle template.

It reads gpsd (which gets a ?WATCH request on connect), raw NMEA, SBP or UBX, and shows what
the source reports.  Nothing is sent to a receiver unless the user turns on NTRIP (--ntrip):
then RTCM corrections from the caster are written to the receiver, on the data link or on the
interface given by --rtcm-out.  gpsview never sends configuration commands.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace
from datetime import datetime, timezone

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, RichLog,
                             Select, Static, TabbedContent, TabPane)

from . import views
from .client import Source, gpsd_feed
from .feeds import Decoder, Event, Feed
from .fmt import DD, DDMM, DDMMSS
from .corrections import Sink, SameLinkSink, SinkSpec, make_sink
from .logfile import RawLog
from .model import GpsState
from .nmea import gga_sentence
from .rtcm import describe
from .ntrip import NtripConfig, NtripSession, make_setup
from .autobaud import detect_baud
from .sources import PROTOCOLS, baud_text, feed_spec, find_ttys, make_feed
from .transports import FILE_RATE, EndOfFile, Transport
from .views import UNITS, StateTimer, ViewOpts

STALL_SECONDS = 30.0     # no data for this long: ask the user what to do
POLL_SECONDS = 1.0       # read timeout; the stall check runs at this rate
NEXT_DEGREES = {DD: DDMM, DDMM: DDMMSS, DDMMSS: DD}
TABS = {"gnss": "tab_gnss", "imu": "tab_imu", "rtk": "tab_rtk"}
MSG_TABS = {"messages": "tab_messages", "counts": "tab_counts"}

HELP = """Help:
c -- clear the messages, or the counts
d -- toggle dd.ddd, dd mm.m and dd mm ss.s
h -- this help
i -- imperial units
m -- metric units
n -- nautical units
o -- choose the input: protocol, source, baud rate
q -- quit
r -- RTK corrections (NTRIP) setup
s -- toggle raw messages output
t -- toggle true/magnetic track
w -- start or stop logging the raw data to a file
1 2 3 -- GNSS, IMU, RTK tabs
4 5 -- Messages, Counts tabs (c clears the one shown)"""


def _age(last: float | None, now: float) -> str:
    if last is None:
        return "-"
    secs = max(now - last, 0.0)
    return f"{secs:.1f} s" if secs < 10 else f"{secs:.0f} s"


def _text(lines: list[str], highlights: tuple[tuple[str, str], ...] = ()) -> Text:
    """Plain lines. 'n/a' is dimmed (the style guide's 'not applicable' colour)."""
    t = Text("\n".join(lines))
    t.highlight_words(["n/a"], "dim")
    for word, style in highlights:
        t.highlight_words([word], style)
    return t


# ----------------------------------------------------------------- dialogs
class HelpScreen(ModalScreen[None]):
    """cgps's 'h' popup. Any key or click closes it."""

    def compose(self) -> ComposeResult:
        with Vertical(id="help"):
            yield Static(Text(HELP))

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss(None)

    def on_click(self) -> None:
        self.dismiss(None)


class ConnectionScreen(ModalScreen[str]):
    """Shown when the source cannot be reached, or the stream stops. Dismisses with
    'retry', 'wait', 'detect', 'configure' or 'quit'."""

    def __init__(self, title: str, detail: str, can_wait: bool,
                 retry_label: str = "Retry", can_detect: bool = False) -> None:
        super().__init__()
        self._title, self._detail, self._can_wait = title, detail, can_wait
        self._retry_label = retry_label
        self._can_detect = can_detect

    def compose(self) -> ComposeResult:
        with Vertical(id="conn"):
            yield Label(Text(self._title, style="bold yellow"))
            yield Static(Text(self._detail))
            with Horizontal():
                yield Button(self._retry_label, id="retry", variant="primary")
                if self._can_wait:
                    yield Button("Wait", id="wait")
                if self._can_detect:
                    yield Button("Auto-baud", id="detect", variant="warning")
                yield Button("Configure another input", id="configure", variant="success")
                yield Button("Quit", id="quit", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "quit")


class SourceScreen(ModalScreen["Feed | None"]):
    """Choose where the data comes from: protocol, source and (for a tty) baud rate.
    Dismisses with a Feed, or None if cancelled."""

    def __init__(self, feed: Feed) -> None:
        super().__init__()
        self._feed = feed
        self._ttys = find_ttys()
        self._auto = ""                      # a suggestion we filled in, safe to replace

    def compose(self) -> ComposeResult:
        feed = self._feed
        spec = feed_spec(feed)
        baud = baud_text(feed)
        with Vertical(id="source"):
            yield Label(Text("Configure input", style="bold"))
            yield Label("Protocol")
            yield Select([(name, key) for name, key in PROTOCOLS], value=feed.protocol or "gpsd",
                         allow_blank=False, compact=True, id="src_proto")
            yield Label("Source", id="src_label")
            yield Input(spec, id="src_spec")
            yield Static("", id="src_hint")
            yield Label("Baud rate  (tty only; blank or auto = detect by listening)")
            yield Input(baud, id="src_baud")
            yield Static("", id="src_ports")
            yield Static("", id="src_error")
            with Horizontal():
                yield Button("Connect", id="connect", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._describe(self.query_one("#src_proto", Select).value)
        ports = "  ".join(self._ttys[:6]) if self._ttys else "none found"
        self.query_one("#src_ports", Static).update(Text(f"Serial ports: {ports}", style="dim"))
        self.query_one("#src_spec", Input).focus()

    def _describe(self, protocol) -> None:
        if protocol == "gpsd":
            label, hint = "gpsd server", "server[:port[:device]]   blank = localhost:2947"
            self.query_one("#src_baud", Input).placeholder = "n/a for gpsd"
        else:
            label = "Source"
            hint = "/dev/ttyUSB0[:baud]   host:port   udp://:port   or a file to replay"
            self.query_one("#src_baud", Input).placeholder = "auto"
        self.query_one("#src_label", Label).update(label)
        self.query_one("#src_hint", Static).update(Text(hint, style="dim"))

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        self._describe(event.value)
        box = self.query_one("#src_spec", Input)
        if (self._auto and box.value == self._auto) or (
                event.value != "gpsd" and box.value == feed_spec(self._feed)
                and self._feed.protocol == "gpsd"):
            self._auto = self._ttys[0] if event.value != "gpsd" and self._ttys else ""
            box.value = self._auto

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._connect()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._connect()

    def _connect(self) -> None:
        protocol = str(self.query_one("#src_proto", Select).value)
        try:
            feed = make_feed(protocol, self.query_one("#src_spec", Input).value,
                             baud=self.query_one("#src_baud", Input).value,
                             file_rate=self._file_rate())
        except ValueError as exc:
            self.query_one("#src_error", Static).update(Text(str(exc), style="bold red"))
            return
        self.dismiss(feed)

    def _file_rate(self) -> float:
        ep = self._feed.endpoint
        return ep.rate if ep is not None else FILE_RATE


class NtripScreen(ModalScreen["tuple[NtripConfig, SinkSpec] | str | None"]):
    """RTK corrections: where to get them and where to send them.

    Dismisses with (config, sink spec) to start, "stop" to stop, or None to close.  What is
    typed here lives in memory only: nothing, the password included, is written to disk."""

    def __init__(self, app_ref: "GpsView") -> None:
        super().__init__()
        self._gv = app_ref

    def compose(self) -> ComposeResult:
        gv = self._gv
        running = gv.ntrip is not None
        cfg = gv.ntrip.cfg if running else None
        url = ""
        if cfg is not None:
            url = f"{'ntrips' if cfg.tls else 'ntrip'}://{cfg.host}:{cfg.port}/{cfg.mountpoint}"
        out = gv.sink.spec.label if running and hasattr(gv.sink, "spec") else "same"
        with Vertical(id="ntrip"):
            yield Label(Text("RTK corrections (NTRIP)", style="bold"))
            yield Static(Text(gv.ntrip.status_text() if running else "Not running."),
                         id="ntrip_state")
            yield Label("Caster  host[:port]/MOUNT  (ntrips:// for TLS)")
            yield Input(url, id="ntrip_url", placeholder="caster.example.com:2101/MOUNT")
            yield Label("User")
            yield Input(cfg.user if cfg else "", id="ntrip_user")
            yield Label("Password  (empty: the NTRIP_PASSWORD variable)")
            yield Input(cfg.password if cfg else "", id="ntrip_pass", password=True)
            yield Label("Send my position every N seconds  (0 = never; VRS needs ~10)")
            yield Input(f"{cfg.gga_interval:g}" if cfg else "0", id="ntrip_gga")
            yield Label("Corrections go to  same | /dev/ttyX[:baud] | host:port | udp://host:port")
            yield Input(out, id="ntrip_out")
            yield Static("", id="ntrip_error")
            with Horizontal():
                yield Button("Restart" if running else "Start", id="start", variant="primary")
                yield Button("Stop", id="stop", disabled=not running)
                yield Button("Close", id="close")

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "stop":
            self.dismiss("stop")
        elif event.button.id == "close":
            self.dismiss(None)
        else:
            self._start()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._start()

    def _start(self) -> None:
        gv = self._gv
        value = lambda i: self.query_one(i, Input).value.strip()
        try:
            gga = float(value("#ntrip_gga") or "0")
        except ValueError:
            self._error("Position interval must be a number of seconds")
            return
        cfg = gv.ntrip.cfg if gv.ntrip else None
        password = value("#ntrip_pass") or None           # None: fall back to the environment
        try:
            setup = make_setup(value("#ntrip_url"), value("#ntrip_out") or "same",
                               endpoint=gv.feed.endpoint, protocol=gv.feed.protocol,
                               user=value("#ntrip_user") or None, password=password,
                               version=cfg.version if cfg else 2, gga=gga)
        except ValueError as exc:
            self._error(str(exc))
            return
        self.dismiss(setup)

    def _error(self, text: str) -> None:
        self.query_one("#ntrip_error", Static).update(Text(text, style="bold red"))


# ----------------------------------------------------------------- app
class GpsView(App):
    CSS_PATH = "gpsview.tcss"
    TITLE = "gpsview"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("h", "help", "Help"),
        Binding("question_mark", "help", "Help", show=False),
        Binding("d", "cycle_degrees", "Lat/lon"),
        Binding("i", "set_units('i')", "Units", key_display="i/m/n"),
        Binding("m", "set_units('m')", "Metric", show=False),
        Binding("n", "set_units('n')", "Nautical", show=False),
        Binding("t", "toggle_track", "Track"),
        Binding("o", "input_dialog", "Input"),
        Binding("r", "ntrip_dialog", "RTK"),
        Binding("s", "toggle_raw", "Raw"),
        Binding("w", "toggle_log", "Log"),
        Binding("c", "clear_raw", "Clear"),
        Binding("1", "show_tab('gnss')", "GNSS", show=False),
        Binding("2", "show_tab('imu')", "IMU", show=False),
        Binding("3", "show_tab('rtk')", "RTK", show=False),
        Binding("4", "show_msg_tab('messages')", "Messages", show=False),
        Binding("5", "show_msg_tab('counts')", "Counts", show=False),
    ]

    def __init__(self, source: Source | Feed, *, units: str = "i", deg: str = DD,
                 magnetic: bool = False, raw_on: bool = True, tab: str = "gnss",
                 log_dir: str | None = None, log_at_start: bool = False,
                 ntrip: tuple[NtripConfig, SinkSpec] | None = None) -> None:
        super().__init__()
        # a gpsd Source (the default) or any Feed: NMEA and SBP come from the CLI
        self.feed: Feed = gpsd_feed(source) if isinstance(source, Source) else source
        self.decoder: Decoder | None = None
        self.gps = GpsState()
        self.opts = ViewOpts(units=UNITS[units], deg=deg, magnetic=magnetic)
        self.raw_on = raw_on
        self.start_tab = TABS[tab]
        self.online = False
        self.rx_bytes = 0
        self.msg_counts: dict[str, list] = {}        # message name -> [count, time of the last]
        self._count_rows: dict[str, list[str]] = {}   # the first column of each Counts table, in order
        self._timer = StateTimer()
        self.sub_title = self.feed.label
        self.log_dir = log_dir or os.getcwd()     # the directory the user launched from
        self.raw_log: RawLog | None = None
        self._log_at_start = log_at_start
        self.transport: Transport | None = None      # the data link while it is connected
        self.ntrip: NtripSession | None = None
        self.sink: Sink | None = None
        if ntrip is not None:
            self._build_ntrip(*ntrip)

    def _build_ntrip(self, config: NtripConfig, spec: SinkSpec) -> None:
        self.sink = make_sink(spec, lambda: self.transport)
        self.ntrip = NtripSession(config, self.sink, gga=self._gga, notify=self.note)

    def _run_ntrip(self) -> None:
        assert self.ntrip is not None and self.sink is not None
        self.note(f"NTRIP: {self.ntrip.cfg.label} -> {self.sink.label}", "bold")
        self.run_worker(self.ntrip.run(), group="ntrip")

    def start_ntrip(self, config: NtripConfig, spec: SinkSpec) -> None:
        """Start (or restart) the corrections.  The old session is stopped first."""
        self.stop_ntrip(announce=False)
        self._build_ntrip(config, spec)
        self._run_ntrip()
        self.refresh_views()

    def stop_ntrip(self, announce: bool = True) -> None:
        if self.ntrip is None:
            return
        self.workers.cancel_group(self, "ntrip")      # the session closes its sink as it ends
        self.ntrip = self.sink = None
        if announce:
            self.note("NTRIP: stopped", "bold")
        self.refresh_views()

    def action_ntrip_dialog(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return

        def done(result) -> None:
            if result == "stop":
                self.stop_ntrip()
            elif result:
                self.start_ntrip(*result)
        self.push_screen(NtripScreen(self), done)

    def action_input_dialog(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return

        def done(feed: Feed | None) -> None:
            if feed is not None:
                self._adopt(feed)
                self.run_worker(self._session(), exclusive=True, group="feed")   # replaces the old
        self.push_screen(SourceScreen(self.feed), done)

    def _is_tty(self) -> bool:
        ep = self.feed.endpoint
        return ep is not None and ep.kind == "serial"

    async def _detect(self, feed: Feed) -> Feed | None:
        """Listen at the usual baud rates and return the feed at the one that works."""
        ep = feed.endpoint
        assert ep is not None
        self.note(f"Detecting the baud rate of {ep.path} (listening, sending nothing) ...", "bold")
        result = await detect_baud(ep.path, feed.protocol,
                                   progress=lambda b: self.note(f"  trying {b} baud ...", "dim"))
        if result.baud is None:
            self._detect_failure = result.summary()
            self.note(self._detect_failure, "red")
            return None
        self.note(f"Detected {result.baud} baud", "green")
        return make_feed(feed.protocol, ep.path, baud=result.baud)

    _detect_failure = ""

    def _adopt(self, feed: Feed, announce: bool = True) -> None:
        """Make `feed` the input: forget the old receiver's state, keep the settings."""
        self.feed = feed
        self.sub_title = feed.label
        self.gps = GpsState()
        self.rx_bytes = 0
        self.msg_counts.clear()
        self.online = False
        if self.raw_log is not None:
            self._stop_log()                 # the log is named for the protocol
        if isinstance(self.sink, SameLinkSink) and (
                feed.protocol == "gpsd" or (feed.endpoint and feed.endpoint.kind == "file")):
            self.stop_ntrip(announce=False)
            self.note("NTRIP stopped: the new input cannot take corrections (press r)", "yellow")
        if announce:
            self.note(f"Input: {feed.title} at {feed.label}", "bold")
        self.refresh_views()

    async def _ask(self, title: str, detail: str, *, can_wait: bool = False,
                   retry_label: str = "Retry", can_detect: bool = False) -> str:
        """The connection dialog.  "Configure another input" opens the source dialog; choosing
        a source returns "retry" (with self.feed changed), cancelling shows this dialog again."""
        while True:
            choice = await self.push_screen_wait(
                ConnectionScreen(title, detail, can_wait, retry_label, can_detect))
            if choice == "detect":
                self._adopt(make_feed(self.feed.protocol, feed_spec(self.feed), baud="auto"))
                return "retry"
            if choice != "configure":
                return choice
            feed = await self.push_screen_wait(SourceScreen(self.feed))
            if feed is not None:
                self._adopt(feed)
                return "retry"

    def _gga(self) -> bytes | None:
        """The rover's position as a GGA sentence, for a caster that wants it."""
        g = self.gps
        return gga_sentence(g.fix, g.sats_used, g.dop.hdop, datetime.now(timezone.utc))

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status")
        with TabbedContent(initial=self.start_tab, id="views"):
            with TabPane("GNSS", id="tab_gnss"):
                with Horizontal():
                    with VerticalScroll(id="datawin"):
                        yield Static(id="datatext")
                    yield DataTable(id="sats", cursor_type="none", zebra_stripes=True)
            with TabPane("IMU", id="tab_imu"):
                with Horizontal():
                    yield Static(id="imu_att", classes="box")
                    yield Static(id="imu_imu", classes="box")
            with TabPane("RTK", id="tab_rtk"):
                with Horizontal():
                    yield Static(id="rtk_att", classes="box")
                    yield Static(id="rtk_tpv", classes="box")
        with TabbedContent(initial="tab_messages", id="msgtabs"):
            with TabPane("Messages", id="tab_messages"):
                yield RichLog(id="messages", max_lines=2000, wrap=False, markup=False)
            with TabPane("Counts", id="tab_counts"):
                with Horizontal():
                    yield DataTable(id="counts", cursor_type="none", zebra_stripes=True)
                    yield DataTable(id="rtcm_counts", cursor_type="none", zebra_stripes=True)
        yield Footer()

    # ----------------------------------------------------------- startup
    def on_mount(self) -> None:
        self.theme = "nord"                    # the default; Ctrl+P changes it
        self.query_one("#sats", DataTable).add_columns("GNSS", "Sig", "PRN", "Elev", "Azim",
                                                       "SNR", "Use")
        self.query_one("#datawin").border_title = "GNSS"
        self.query_one("#sats").border_title = "Seen  0/Used  0"
        self.query_one("#imu_att").border_title = "ATT"
        self.query_one("#imu_imu").border_title = "IMU"
        self.query_one("#rtk_att").border_title = "ATT"
        self.query_one("#rtk_tpv").border_title = "TPV"
        for table, columns in (("#counts", (("Receiver", "message"), ("Count", "count"),
                                            ("Last", "last"))),
                               ("#rtcm_counts", (("RTCM", "type"), ("Name", "name"),
                                                 ("In", "in"), ("Out", "out"),
                                                 ("Used", "used"), ("Last in", "last")))):
            for label, key in columns:
                self.query_one(table, DataTable).add_column(label, key=key)
        self.note(f"Connecting to {self.feed.title} at {self.feed.label}")
        self.run_worker(self._session(), exclusive=True, group="feed")
        if self.ntrip is not None:
            self._run_ntrip()
        self.set_interval(1.0, self.refresh_views)
        if self._log_at_start:
            self.action_toggle_log()
        self.refresh_views()

    def on_unmount(self) -> None:
        if self.raw_log is not None:
            self.raw_log.close()

    # ------------------------------------------------------------ helpers
    def _log(self) -> RichLog | None:
        try:
            return self.screen_stack[0].query_one("#messages", RichLog)
        except (NoMatches, IndexError):      # not composed yet, or the app is shutting down
            return None

    def note(self, msg: str, style: str = "") -> None:
        log = self._log()
        if log is not None:
            log.write(Text(msg, style=style))

    # ------------------------------------------------------- feed session
    async def _session(self) -> None:
        """Connect, read, and reconnect. Every dialog is shown from here (it is a worker)."""
        while True:
            feed = self.feed                 # may have been changed by "Configure another input"
            if feed.endpoint is not None and feed.endpoint.autobaud:
                found = await self._detect(feed)
                if found is None:
                    choice = await self._ask(
                        "No baud rate found", self._detect_failure,
                        retry_label="Detect again")
                    if choice == "quit":
                        self.exit()
                        return
                    continue
                self._adopt(found, announce=False)      # now a plain rate: reconnects keep it
                feed = self.feed
            transport: Transport = feed.make_transport()
            self.decoder = feed.make_decoder()
            try:
                try:
                    await transport.open()
                except asyncio.CancelledError:      # the input was changed while connecting
                    await transport.close()
                    raise
                self.transport = transport
                command = self.decoder.on_open()
                if command:
                    await transport.write(command)
            except OSError as exc:
                self.transport = None
                await transport.close()
                self.note(f"Cannot connect to {feed.title}: {exc}", "red")
                choice = await self._ask(
                    f"{feed.title} is not answering",
                    f"{exc}\n\nIs the source running and reachable? ({feed.label})\n"
                    "You can retry, quit, or configure another input.")
            else:
                self.note(f"Connected to {feed.title} at {feed.label}", "green")
                try:
                    choice = await self._read_until_done(transport, self.decoder)
                except EndOfFile as exc:     # a replayed file ran out: not a failed link
                    self.online, self.transport = False, None
                    self.note(f"{exc}", "yellow")
                    choice = await self._ask(
                        "End of file", f"{exc}\n\nReplay starts again from the beginning.",
                        retry_label="Replay")
                except OSError as exc:
                    self.online, self.transport = False, None    # no corrections to a dead link
                    self.note(f"Connection to {feed.title} lost: {exc}", "red")
                    choice = await self._ask(
                        f"Connection to {feed.title} was lost",
                        f"{exc}\n\nLast device: {self.gps.device or '-'}")
                finally:
                    self.transport = None            # before it closes: no corrections to it
                    await transport.close()
                    self.online = False
            if choice == "quit":
                self.exit()
                return
            self.note("Retrying connection ...", "yellow")

    async def _read_until_done(self, transport: Transport, decoder: Decoder) -> str:
        """Read until the link fails (OSError) or the user chooses."""
        last = time.monotonic()
        while True:
            data = await transport.read(POLL_SECONDS)
            if data is None:
                if time.monotonic() - last >= STALL_SECONDS:
                    self.note(f"No data from {self.feed.title} for {STALL_SECONDS:.0f} s",
                              "yellow")
                    choice = await self._ask(
                        f"No data from {self.feed.title} for {STALL_SECONDS:.0f} s",
                        "It may have stopped, or the link hung. A receiver serves one "
                        "client at a time: if another phone or PC just connected, the "
                        "stream moved to it. For a tty, a wrong baud rate looks like this: "
                        "Auto-baud listens at the usual rates and picks the one that works.",
                        can_wait=True, can_detect=self._is_tty())
                    if choice == "wait":
                        last = time.monotonic()
                        continue
                    return choice
                continue
            last = time.monotonic()
            self.online = True
            self.rx_bytes += len(data)
            if self.raw_log is not None:
                try:
                    self.raw_log.write(data)
                except OSError as exc:
                    self.note(f"Logging stopped, write failed: {exc}", "red")
                    self._stop_log()
            self._handle_events(decoder.feed(data))

    def _handle_events(self, events: list[Event]) -> None:
        for ev in events:
            if ev.kind == "raw":
                if ev.msg:
                    stat = self.msg_counts.setdefault(ev.msg, [0, 0.0])
                    stat[0] += 1
                    stat[1] = time.monotonic()
                if self.raw_on:
                    log = self._log()
                    if log is not None:
                        log.write(Text(ev.text, style=ev.style))
            elif ev.kind == "obj" and ev.obj is not None:
                self.gps.apply(ev.obj)
            elif ev.kind == "note":
                self.note(ev.text, ev.style)

    # ------------------------------------------------------------ refresh
    def refresh_views(self) -> None:
        try:
            self._refresh_views()
        except (NoMatches, IndexError):      # not composed yet, or shutting down
            pass

    def _refresh_views(self) -> None:
        self._refresh_counts()
        base = self.screen_stack[0]
        now = datetime.now(timezone.utc)
        g, opts = self.gps, self.opts
        secs = self._timer.seconds(0 if not self.online else g.fix.mode)

        base.query_one("#datatext", Static).update(_text(
            views.gnss_lines(g, opts, online=self.online, secs=secs, now=now)))

        table = base.query_one("#sats", DataTable)
        table.clear()
        for row in views.sat_rows(g.sats):
            table.add_row(*row)
        table.border_title = f"Seen {g.sats_seen:2d}/Used {g.sats_used:2d}"

        d = self.decoder
        errors = f"{d.error_label} {d.errors}" if d is not None and d.error_label else ""
        status = views.status_lines(g, opts, link=self.feed.label, online=self.online,
                                    secs=secs, rx_bytes=self.rx_bytes, raw_on=self.raw_on,
                                    header=self.feed.header, errors=errors,
                                    log_on=self.raw_log is not None, ntrip=self._ntrip_line())
        bar = base.query_one("#status", Static)
        bar.styles.height = len(status)
        bar.update(_text(status, (("OFFLINE", "bold red"),)))

        base.query_one("#imu_att", Static).update(_text(
            views.imu_lines(g.attitude, attitude=True)))
        base.query_one("#imu_imu", Static).update(_text(
            views.imu_lines(g.imu, attitude=False)))
        base.query_one("#rtk_att", Static).update(_text(
            views.rtk_att_lines(g.attitude)))
        base.query_one("#rtk_tpv", Static).update(_text(
            views.rtk_tpv_lines(g.fix)))

    def _ntrip_line(self) -> str:
        if self.ntrip is None:
            return ""
        d = self.decoder
        report = ""          # what the receiver says about the corrections, where it says
        if d is not None and hasattr(d, "rtcm_ok") and (d.rtcm_ok or d.rtcm_failed):
            report = f"receiver: RTCM ok {d.rtcm_ok} bad {d.rtcm_failed}"
        return self.ntrip.status_text(report, max(self.size.width - 2, 20))

    # ------------------------------------------------------------ actions
    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_cycle_degrees(self) -> None:
        self.opts = replace(self.opts, deg=NEXT_DEGREES[self.opts.deg])
        self.refresh_views()

    def action_set_units(self, letter: str) -> None:
        self.opts = replace(self.opts, units=UNITS[letter])
        self.refresh_views()

    def action_toggle_track(self) -> None:
        self.opts = replace(self.opts, magnetic=not self.opts.magnetic)
        self.refresh_views()

    def action_toggle_raw(self) -> None:
        self.raw_on = not self.raw_on
        self.note(f"Raw messages {'on' if self.raw_on else 'off'}", "bold")

    def action_toggle_log(self) -> None:
        if self.raw_log is not None:
            self._stop_log()
            return
        try:
            self.raw_log = RawLog(self.log_dir, self.feed.title.lower())
        except OSError as exc:
            self.note(f"Cannot start logging in {self.log_dir}: {exc}", "red")
            return
        self.note(f"Logging raw data to {self.raw_log.path}", "bold")
        self.refresh_views()

    def _stop_log(self) -> None:
        log, self.raw_log = self.raw_log, None
        if log is not None:
            log.close()
            self.note(f"Logging stopped: {log.path} ({log.bytes_written} bytes)", "bold")
        self.refresh_views()

    def action_clear_raw(self) -> None:
        """c: clear whichever of Messages and Counts is showing."""
        try:
            counts = self.screen_stack[0].query_one("#msgtabs", TabbedContent).active == "tab_counts"
        except (NoMatches, IndexError):
            return
        if counts:
            self.msg_counts.clear()
            self._count_rows.clear()
            if self.ntrip is not None:
                for table in (self.ntrip.types, self.ntrip.types_out, self.ntrip.seen_in,
                              self.ntrip.seen_out):
                    table.clear()
            self.refresh_counts()
            return
        log = self._log()
        if log is not None:
            log.clear()

    def action_show_tab(self, tab: str) -> None:
        self.screen_stack[0].query_one("#views", TabbedContent).active = TABS[tab]

    def action_show_msg_tab(self, tab: str) -> None:
        self.screen_stack[0].query_one("#msgtabs", TabbedContent).active = MSG_TABS[tab]
        self.refresh_counts()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id == "msgtabs":
            self.refresh_counts()

    # ------------------------------------------------------------ message counts
    def _receiver_rows(self) -> list[tuple[str, ...]]:
        now = time.monotonic()
        return [(name, str(c), _age(last, now)) for name, (c, last) in sorted(self.msg_counts.items())]

    def _rtcm_rows(self) -> list[tuple[str, ...]]:
        """One row per RTCM message type: how many came in from the caster, how many were
        delivered to the receiver, and how long ago the last one arrived."""
        s = self.ntrip
        if s is None:
            return []
        now = time.monotonic()
        reported = getattr(self.decoder, "rtcm_types", {})   # the receiver's own verdicts
        return [(str(t), describe(t), str(s.types[t]), str(s.types_out.get(t, 0)),
                 str(reported[t][1]) if t in reported else "-", _age(s.seen_in.get(t), now))
                for t in sorted(s.types)]

    def refresh_counts(self) -> None:
        try:
            self._refresh_counts()
        except (NoMatches, IndexError):
            pass

    def _refresh_counts(self) -> None:
        base = self.screen_stack[0]
        if base.query_one("#msgtabs", TabbedContent).active != "tab_counts":
            return                                        # nobody is looking: no work
        self._fill(base.query_one("#counts", DataTable), self._receiver_rows(), "receiver",
                   ("count", "last"))
        self._fill(base.query_one("#rtcm_counts", DataTable), self._rtcm_rows(), "rtcm",
                   ("in", "out", "used", "last"))

    def _fill(self, table: DataTable, rows: list[tuple[str, ...]], which: str,
              live: tuple[str, ...]) -> None:
        """Show `rows`.  A new first column value means a new row, so the table is rebuilt (and
        keeps its scroll position); otherwise the numbers are updated in place."""
        keys = [r[0] for r in rows]
        if keys != self._count_rows.get(which):
            scroll = table.scroll_y
            table.clear()
            for row in rows:
                table.add_row(*row, key=row[0])
            self._count_rows[which] = keys
            table.scroll_y = scroll
            return
        names = [c.key.value for c in table.columns.values()]
        for row in rows:
            for column in live:
                table.update_cell(row[0], column, row[names.index(column)], update_width=False)
