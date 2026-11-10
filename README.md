# gpsview

A Textual clone of **cgps**, the curses gpsd client in `~/dev/gpsd/clients/cgps.c`.
It shows the same fix, error estimates, satellites and raw data, and it is built on the
TUI style in `~/dev/styles/tuiStyle`.

It reads from three sources:

* **gpsd** (the default), as cgps does
* **raw NMEA 0183**, with `-n`, bypassing gpsd
* **Swift Binary Protocol (SBP)**, with `-sbp`, bypassing gpsd

Every source can be a serial tty, a TCP connection, or UDP.

It is read-only. It never sends configuration to a receiver. The only bytes it writes
are gpsd's `?WATCH` request, and only when the source is gpsd.

## Setup

    cd ~/dev/gpsview
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt     # textual, pytest, pyserial, sbp

Needs Python 3.11 or later. Tested with Textual 8.2.8, pyserial 3.5 and sbp 6.5.2 on
Python 3.14. The `sbp` package decodes SBP payloads; `pyserial` opens ttys.

## Run

    bin/gpsview                         # gpsd on localhost:2947
    bin/gpsview pi.local                # gpsd on another host
    bin/gpsview pi.local:2947:/dev/ttyACM0
    bin/gpsview -i                      # start on the IMU tab
    bin/gpsview -r                      # start on the RTK tab
    bin/gpsview -sbp                    # SBP from tcp://172.16.0.120:55555
    bin/gpsview -sbp tcp://10.0.0.7:55555
    bin/gpsview -sbp /dev/ttyUSB0       # SBP on a serial port, 115200 baud
    bin/gpsview -n /dev/ttyACM0         # NMEA on a serial port, 9600 baud
    bin/gpsview -n udp://:10110         # NMEA datagrams on UDP port 10110
    bin/gpsview --log                   # start with the raw data being logged to a file
    bin/gpsview --log-dir ~/logs        # put log files somewhere other than where you launch from
    bin/gpsview --help

Options follow cgps: `-i` `-r` `-l {d,m,s}` `-m` `-s` `-u {i,m,n}` `-V`, plus `-n`, `-sbp`,
`--log` and `--log-dir`. Give one source only: a gpsd server, `-n`, or `-sbp`.

| Key | Action |
| --- | --- |
| `h` or `?` | Help popup (any key closes it) |
| `q` | Quit |
| `d` | Cycle lat/lon format: DD.dddddddd, DD MM.mmmmmm', DD MM' SS.sssss" |
| `i` `m` `n` | Imperial, metric, nautical units |
| `t` | Show the track as true or magnetic |
| `s` | Raw messages on or off |
| `c` | Clear the raw pane |
| `w` | Start or stop logging the raw data to a file |
| `1` `2` `3` | GNSS, IMU, RTK tab (the tabs are also clickable) |

The GNSS tab has the cgps data panel on the left and the satellite table on the
right. The table lists used satellites first, then by PRN. Its title shows
`Seen/Used`, which counts satellites, not table rows.

The table has one row per signal. SBP reports a satellite once per tracked signal, so a
satellite tracked on L1 and on L2 has two rows, `L1CA` and `L2CM`, with the same
elevation and azimuth. The count is still one satellite. The `Sig` column is the signal
name for SBP, and gpsd's signal id (as cgps shows it) for gpsd. NMEA has no signal
column.

The bottom pane is the **messages** pane: the raw messages from the source (dim: gpsd
JSON, NMEA sentences, or one summary line per SBP frame), and notices in colour:
connection state, the protocol in use, and warnings. `s` turns the raw messages off;
notices still show.

## Sources

### gpsd

`server[:port[:device]]`, as cgps. `?WATCH` is sent on connect. The raw pane shows gpsd's
JSON.

### NMEA 0183 (`-n`)

Each sentence is checked against its `*hh` checksum. A sentence that fails is shown in red
and not used, and the status bar counts it as `bad checksums N`.

Sentences used: GGA, RMC, VTG, GSA, GSV, GST, ZDA. The fix comes from GGA and RMC, the
satellites from GSV and GSA, and the error estimates from GST. Talkers GP, GL, GA, GB/BD,
GQ and GI are mapped to gpsd's constellation numbers.

### Swift Binary Protocol (`-sbp`)

Frames start with 0x55 and end with a CRC-16/XMODEM. A frame with a bad CRC is counted
(`bad CRC N`) and the decoder resynchronises on the next 0x55.

Messages used:

| Message | Shown as |
| --- | --- |
| POS_LLH 0x020A | fix, position, altitude (HAE), position accuracy |
| UTC_TIME 0x0103 | fix time |
| VEL_NED 0x020E | speed, track, climb |
| DOPS 0x0208 | PDOP, HDOP, VDOP, TDOP, GDOP |
| SV_AZ_EL 0x0097 | satellite elevation and azimuth |
| MEASUREMENT_STATE 0x0061 | satellite SNR (cn0), and which satellites are tracked |
| BASELINE_NED 0x020C | RTK base: status, East, North, Up, length, course |

Other messages are listed in the raw pane as one summary line each (for example
`SBP PosLLH 0x020A sender 1 34 bytes`), so the stream is visible even where the
decoder does not use the message.

The SBP default is `tcp://172.16.0.120:55555`. The source is read-only: nothing is sent to
the receiver.

### Source forms

| Form | Meaning | Default baud |
| --- | --- | --- |
| `/dev/ttyUSB0` | serial tty | NMEA 9600, SBP 115200 |
| `/dev/ttyUSB0:4800` | serial tty at a given baud | |
| `host:port` or `tcp://host:port` | TCP client | |
| `udp://host:port` | UDP: bind `host` (a local address) and read | |
| `udp://:port` | UDP on all interfaces | |
| `[ipv6]:port`, `udp://[ipv6]:port` | IPv6 (brackets are required) | |

A serial tty needs read access to the device, usually membership of the `dialout` (or
`uucp`) group.

### Logging

`w` starts logging the raw data to a file, and `w` again stops it. `--log` starts logging
as soon as gpsview starts. The file holds the bytes exactly as they arrived, so SBP
frames are kept intact and can be decoded again later.

The file name is `gpsview_<protocol>_yyyymmdd_hhmmZ.log`, where the protocol is `gpsd`,
`nmea` or `sbp`, and the time is UTC. For example `gpsview_sbp_20261110_1343Z.log`. The
file goes in the directory you launched from, or in `--log-dir DIR`. If a file with the
same name already exists (two sessions in the same minute), the new one is
`..._1343Z_2.log`, and an existing file is never overwritten.

The status bar shows `log on` or `log off`. The messages pane names the file when
logging starts and gives its size when logging stops.

## Connection behaviour

* If the source cannot be reached, a dialog offers **Retry** or **Quit**.
* If the connection drops, the same dialog appears.
* If no data arrives for 30 s, the dialog offers **Keep waiting**, **Retry** or
  **Quit**. A receiver serves one client at a time, so another phone or PC may have
  taken the stream.
* For gpsd, a different API version from the one this client was written against
  (16.1) shows a yellow warning.

## Try it without hardware

`tools/fake_gpsd.py` is a simulated gpsd on TCP:

    .venv/bin/python tools/fake_gpsd.py            # port 2947, a 3D fix moving in a small circle
    .venv/bin/python tools/fake_gpsd.py --no-fix   # NO FIX
    bin/gpsview localhost:2947

`tools/fake_feeds.py` simulates a raw NMEA or SBP receiver. It runs on TCP, UDP, or a
pty that stands in for a serial port:

    .venv/bin/python tools/fake_feeds.py nmea --tcp 10110
    bin/gpsview -n tcp://127.0.0.1:10110

    .venv/bin/python tools/fake_feeds.py sbp --udp 10112
    bin/gpsview -sbp udp://:10112

    .venv/bin/python tools/fake_feeds.py sbp --pty    # prints the tty name
    bin/gpsview -sbp /dev/pts/3                        # use the name it printed

## Limitations

These are the places where a source does not give what the view shows. Each shows
`n/a` rather than a guess.

* **NMEA**: no ECEF, no leap seconds, no heading (HDT is not decoded), no DOPs other
  than PDOP/HDOP/VDOP, no climb unless the receiver sends it in another sentence.
* **SBP**: no heading or attitude (the ORIENT messages are not decoded), so the ATT box
  on the IMU tab is empty. IMU_RAW (0x0900) is not decoded, so the IMU tab is empty too.
  No MSL altitude (POS_LLH gives HAE only), no ECEF, no leap seconds.
* **Used satellites** mean something different for each source:
  * gpsd: its own `used` flag.
  * NMEA: the satellites listed in the GSA sentences of the epoch, one GSA per
    constellation, combined. On the Swift receiver at 172.16.0.120 that is 30, while the
    GGA satellite count (the number in the solution) is 22.
  * SBP: "used" means tracked (cn0 > 0), so it is larger than the solution. The solution
    count is `n_sats` in POS_LLH, which is 22 on the same receiver, matching GGA and the
    Swift Navigation console. That 22 is the number to read as "used". No SBP satellite
    message says which satellites are in the solution.
* **GLONASS** in SBP: the satellite number is the slot (1 to 28), shown as PRN 64 + slot
  to follow gpsd. Frequency-channel forms (100 + FCN) are not listed.
* **Signal codes**: the constellation for each SBP satellite comes from the `code` field,
  using libsbp's spec (`spec/yaml/swiftnav/sbp/gnss.yaml`). Codes not in that table are
  not listed.

## Layout and style

The app follows `~/dev/styles/tuiStyle/STYLE.md`:

* header, a 3-line status bar on `$boost`, and tabs, with a message pane and a footer
* round `$primary` borders, and theme colours only in the stylesheet
* semantic colours: dim for `n/a`, red for OFFLINE, and the same colours for dialogs
* the connection dialog from the style guide's dead-link section

Changes from the template, which are deliberate:

* **No write path.** The style's `_write()`, confirm dialog and read-back are not
  needed, because the app never changes the receiver. The bindings are cgps's, not
  `g`/`p`/`c`.
* **The message pane is shared.** The style's command log is replaced by one pane
  holding raw data and notices, as cgps's messages window does. It takes the rows left
  over after the tabs (at least 5), so 80x24 terminals still show data.

## Differences from cgps

cgps is the reference. These are the places where gpsview differs on purpose.

| cgps | gpsview | Why |
| --- | --- | --- |
| EPD (track error, in degrees) is multiplied by the speed factor (mph, knots or km/h) | EPD is shown in degrees | cgps passes `speedfactor` for EPD. That is a bug. |
| sigid 10 to 12 is printed as `:` `;` `<` (ASCII after `9`) | shown as `A` `B` `C` | Same width, and the characters are readable. |
| Gyro rates are labelled `deg/s^2` | `deg/s` | The unit of a gyro rate is deg/s. |
| "ATT" Gyro T / Gyro X come from libgps, which writes the `gyro_temp` key into `gyro_x` (`libgps/libgps_json.c`, the ATT table) | `gyro_temp` is read into Gyro T | libgps bug. The IMU table in the same file is correct. |
| The satellite list and DOP/ECEF blocks are cut when the window is short, with "More..." | Everything is shown and the panel scrolls | Simpler than fitting by terminal height. |
| Satellite table fits beside the data panel at 80 columns | Needs about 100 columns for all columns; narrower terminals scroll it sideways | The Textual table has padding. |
| `s` hides the message window (`-s` silences it) | `s` turns raw data off; notices still show | The message pane is also where notices go. |
| Version mismatch: warning, then a 4 s pause | Warning only | No pause in a UI. |
| Cannot connect: prints an error and exits | Dialog with Retry or Quit | Lets you start the source and carry on. |
| No data for 2 minutes: exits | Dialog after 30 s with Keep waiting | Same idea, shorter, and it asks first. |
| IMU and RTK come only from `-i` / `-r` | Also switchable with `1` `2` `3` | Tabs in the style. |
| Leap seconds are shown as `(0)` if gpsd omits them | Shown as `(n/a)` | Not every source has them. |
| "SNARD" when there is no ATT time | `n/a` | Joke text. |
| `-D` debug level for libgps | Not implemented | Not needed for a Textual client. |

## Files

    bin/gpsview              launcher (uses .venv if present)
    gpsview/__main__.py      command line: gpsd server, -n, -sbp
    gpsview/app.py           the Textual app, key bindings, dialogs, the read loop
    gpsview/feeds.py         Feed (transport + decoder), decoder events
    gpsview/transports.py    TCP, UDP and serial transports; endpoint parsing
    gpsview/client.py        gpsd: server string, ?WATCH, JSON decoder
    gpsview/nmea.py          NMEA 0183 decoder -> TPV, SKY
    gpsview/swift.py         SBP framing, CRC, decoder -> TPV, SKY, ATT
    gpsview/logfile.py       the raw data log (gpsview_<protocol>_yyyymmdd_hhmmZ.log)
    gpsview/model.py         decoded objects -> plain state (no I/O)
    gpsview/fmt.py           number and coordinate formatting, ported from C
    gpsview/views.py         panel text: GNSS, satellites, IMU, RTK, status bar
    gpsview/gpsview.tcss     stylesheet
    tools/fake_gpsd.py       simulated gpsd for demos and tests
    tools/fake_feeds.py      simulated NMEA and SBP receivers (TCP, UDP, pty)
    tools/golden/            regenerates tests/golden_fmt.tsv from the C code
    tests/                   unit, golden and end-to-end tests

## Tests

    .venv/bin/python -m pytest -q

* `test_fmt.py` checks 276 formatter outputs against the C code. The C is taken
  from `~/dev/gpsd` and compiled with gcc. To regenerate the golden file after
  changing the gpsd source:

      tools/golden/gen_golden.sh ~/dev/gpsd

* `test_model.py`, `test_client.py` and `test_views.py` cover parsing, the gpsd server
  string, and the panel text.
* `test_transports.py` covers the endpoint forms, UDP loopback, TCP, and a pty standing
  in for a serial port.
* `test_nmea.py` covers checksums, each sentence, epochs, malformed input, and input
  split across reads. The sample sentences are the published NMEA examples, with
  their checksums checked.
* `test_swift.py` builds frames with the `sbp` package's own encoder and covers framing,
  CRC errors, resynchronisation, and each mapping.
* `test_app.py` runs the Textual app against `tools/fake_gpsd.py` over TCP. It covers
  keys, the help popup, no fix, a refused connection, a dropped connection and retry,
  and the stall dialog.
* `test_feeds.py` runs the app against the fake NMEA and SBP receivers over TCP, UDP
  and a pty.
* `test_cli.py` covers the source options and the refusals (two sources, a bad spec).
* `test_logfile.py` covers the log file name (UTC), the suffix for a second file in the
  same minute, and the app writing the received bytes.

The NMEA and SBP sources have been checked against simulated receivers. The SBP source
was also checked against the live endpoint `tcp://172.16.0.120:55555`. There was no
NMEA TCP stream to test against.
