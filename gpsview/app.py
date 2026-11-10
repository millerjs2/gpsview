"""gpsview: a Textual clone of cgps.  Layout and dialogs follow ~/dev/styles/tuiStyle.

The app is read-only.  It reads gpsd (which gets a ?WATCH request on connect), raw NMEA,
or SBP, and shows what the source reports.  Nothing is sent to a receiver.
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
from textual.widgets import (Button, DataTable, Footer, Header, Label, RichLog, Static,
                             TabbedContent, TabPane)

from . import views
from .client import Source, gpsd_feed
from .feeds import Decoder, Event, Feed
from .fmt import DD, DDMM, DDMMSS
from .logfile import RawLog
from .model import GpsState
from .transports import Transport
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

    def __init__(self, title: str, detail: str, can_wait: bool) -> None:
        super().__init__()
        self._title, self._detail, self._can_wait = title, detail, can_wait

    def compose(self) -> ComposeResult:
        with Vertical(id="conn"):
            yield Label(Text(self._title, style="bold yellow"))
            yield Static(Text(self._detail))
            with Horizontal():
                yield Button("Retry connection", id="retry", variant="primary")
                if self._can_wait:
                    yield Button("Keep waiting", id="wait")
                yield Button("Quit", id="quit", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "quit")


# ----------------------------------------------------------------- app
class GpsView(App):
    CSS_PATH = "gpsview.tcss"
    TITLE = "gpsview"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("h", "help", "Help"),
        Binding("question_mark", "help", "Help", show=False),
        Binding("d", "cycle_degrees", "Lat/lon format"),
        Binding("i", "set_units('i')", "Imperial"),
        Binding("m", "set_units('m')", "Metric"),
        Binding("n", "set_units('n')", "Nautical"),
        Binding("t", "toggle_track", "True/mag track"),
        Binding("s", "toggle_raw", "Raw messages"),
        Binding("w", "toggle_log", "Log"),
        Binding("c", "clear_raw", "Clear raw"),
        Binding("1", "show_tab('gnss')", "GNSS", show=False),
        Binding("2", "show_tab('imu')", "IMU", show=False),
        Binding("3", "show_tab('rtk')", "RTK", show=False),
    ]

    def __init__(self, source: Source | Feed, *, units: str = "i", deg: str = DD,
                 magnetic: bool = False, raw_on: bool = True, tab: str = "gnss",
                 log_dir: str | None = None, log_at_start: bool = False) -> None:
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
        except NoMatches:
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
                command = self.decoder.on_open()
                if command:
                    await transport.write(command)
            except OSError as exc:
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
                except OSError as exc:
                    self.online = False
                    self.note(f"Connection to {feed.title} lost: {exc}", "red")
                    choice = await self.push_screen_wait(ConnectionScreen(
                        f"Connection to {feed.title} was lost",
                        f"{exc}\n\nLast device: {self.gps.device or '-'}", can_wait=False))
                finally:
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
        except NoMatches:            # not composed yet, or shutting down
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
                                    log_on=self.raw_log is not None)
        base.query_one("#status", Static).update(_text(status, (("OFFLINE", "bold red"),)))

        base.query_one("#imu_att", Static).update(_text(
            views.imu_lines(g.attitude, attitude=True)))
        base.query_one("#imu_imu", Static).update(_text(
            views.imu_lines(g.imu, attitude=False)))
        base.query_one("#rtk_att", Static).update(_text(
            views.rtk_att_lines(g.attitude)))
        base.query_one("#rtk_tpv", Static).update(_text(
            views.rtk_tpv_lines(g.fix)))

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
