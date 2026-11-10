"""gpsview: a Textual clone of cgps.  Layout and dialogs follow the tuiStyle template.

It reads gpsd (which gets a ?WATCH request on connect), raw NMEA, SBP or UBX, and shows what
the source reports.  Nothing is sent to a receiver unless the user turns on NTRIP (--ntrip):
then RTCM corrections from the caster are written to the receiver, on the data link or on the
interface given by --rtcm-out.  gpsview never sends configuration commands.
"""
from __future__ import annotations

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
                             Static, TabbedContent, TabPane)

from . import views
from .client import Source, gpsd_feed
from .feeds import Decoder, Event, Feed
from .fmt import DD, DDMM, DDMMSS
from .corrections import Sink, SinkSpec, make_sink
from .logfile import RawLog
from .model import GpsState
from .nmea import gga_sentence
from .ntrip import NtripConfig, NtripSession, make_setup
from .transports import EndOfFile, Transport
from .views import UNITS, StateTimer, ViewOpts

STALL_SECONDS = 30.0     # no data for this long: ask the user what to do
POLL_SECONDS = 1.0       # read timeout; the stall check runs at this rate
NEXT_DEGREES = {DD: DDMM, DDMM: DDMMSS, DDMMSS: DD}
TABS = {"gnss": "tab_gnss", "imu": "tab_imu", "rtk": "tab_rtk"}

HELP = """Help:
c -- clear raw data area
d -- toggle dd.ddd, dd mm.m and dd mm ss.s
h -- this help
i -- imperial units
m -- metric units
n -- nautical units
q -- quit
r -- RTK corrections (NTRIP) setup
s -- toggle raw messages output
t -- toggle true/magnetic track
w -- start or stop logging the raw data to a file
1 2 3 -- GNSS, IMU, RTK tabs"""


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
    'retry', 'wait' or 'quit'."""

    def __init__(self, title: str, detail: str, can_wait: bool,
                 retry_label: str = "Retry connection") -> None:
        super().__init__()
        self._title, self._detail, self._can_wait = title, detail, can_wait
        self._retry_label = retry_label

    def compose(self) -> ComposeResult:
        with Vertical(id="conn"):
            yield Label(Text(self._title, style="bold yellow"))
            yield Static(Text(self._detail))
            with Horizontal():
                yield Button(self._retry_label, id="retry", variant="primary")
                if self._can_wait:
                    yield Button("Keep waiting", id="wait")
                yield Button("Quit", id="quit", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "quit")


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
        Binding("r", "ntrip_dialog", "RTK"),
        Binding("s", "toggle_raw", "Raw"),
        Binding("w", "toggle_log", "Log"),
        Binding("c", "clear_raw", "Clear"),
        Binding("1", "show_tab('gnss')", "GNSS", show=False),
        Binding("2", "show_tab('imu')", "IMU", show=False),
        Binding("3", "show_tab('rtk')", "RTK", show=False),
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
        yield RichLog(id="messages", max_lines=2000, wrap=False, markup=False)
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
        self.query_one("#messages").border_title = "messages"
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
        feed = self.feed
        while True:
            transport: Transport = feed.make_transport()
            self.decoder = feed.make_decoder()
            try:
                await transport.open()
                self.transport = transport
                command = self.decoder.on_open()
                if command:
                    await transport.write(command)
            except OSError as exc:
                self.transport = None
                await transport.close()
                self.note(f"Cannot connect to {feed.title}: {exc}", "red")
                choice = await self.push_screen_wait(ConnectionScreen(
                    f"{feed.title} is not answering",
                    f"{exc}\n\nIs the source running and reachable? ({feed.label})",
                    can_wait=False))
            else:
                self.note(f"Connected to {feed.title} at {feed.label}", "green")
                try:
                    choice = await self._read_until_done(transport, self.decoder)
                except EndOfFile as exc:     # a replayed file ran out: not a failed link
                    self.online, self.transport = False, None
                    self.note(f"{exc}", "yellow")
                    choice = await self.push_screen_wait(ConnectionScreen(
                        "End of file", f"{exc}\n\nReplay starts again from the beginning.",
                        can_wait=False, retry_label="Replay"))
                except OSError as exc:
                    self.online, self.transport = False, None    # no corrections to a dead link
                    self.note(f"Connection to {feed.title} lost: {exc}", "red")
                    choice = await self.push_screen_wait(ConnectionScreen(
                        f"Connection to {feed.title} was lost",
                        f"{exc}\n\nLast device: {self.gps.device or '-'}", can_wait=False))
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
                    choice = await self.push_screen_wait(ConnectionScreen(
                        f"No data from {self.feed.title} for {STALL_SECONDS:.0f} s",
                        "It may have stopped, or the link hung. A receiver serves one "
                        "client at a time: if another phone or PC just connected, the "
                        "stream moved to it.",
                        can_wait=True))
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
        log = self._log()
        if log is not None:
            log.clear()

    def action_show_tab(self, tab: str) -> None:
        self.screen_stack[0].query_one("#views", TabbedContent).active = TABS[tab]
