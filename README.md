# ebc-cli

`ebc-cli` is a small terminal interface for ZKETECH EBC battery testers. It
reads measurements, starts bounded charge/discharge profiles, sends an
immediate stop command, and records status frames as CSV.

The current protocol implementation targets the ZKETECH EBC-A10H (model ID
`0x06`) over a 9600 baud, 8 data bit, even parity, 1 stop bit serial link
(8E1). It also recognizes EBC-A05 status frames (model ID `0x05`) and applies
the source-documented tenfold current scaling so read-only measurements are
meaningful. The A05 control path remains disabled until a real A05 wire
capture validates the shared profile packets. Unknown EBC models remain
diagnostic-only.

## Features

- `status` reads one status frame without changing the tester mode.
- `monitor` streams status frames until interrupted or until `--count` is
  reached.
- `modes` (also `list-modes`) prints the operating-mode catalog imported from
  the EBC-controller `ebcc.init` source without connecting to the tester.
- `plan-mode` builds a source-defined packet for offline inspection only.
- `plan-lead-acid-charge` prints a PbAc charge packet without transmitting it.
- `recover-lead-acid` starts a bounded, supervised PbAc recovery profile.
- `float-charge` starts a bounded 12 V lead-acid CCCV float profile.
- `bulk-to-float` runs a normal PbAc bulk charge, then transitions to a bounded
  CCCV float hold when the tester reports PbAc auto-off.
- `capacity-test` runs a low-rate constant-current discharge and keeps
  recording samples until the tester reaches its cutoff or the safety timeout.
- `stop --execute` sends the immediate EBC stop frame.
- `report` replays a CSV session without connecting to the tester and reports
  host-integrated Ah/Wh totals.

Commands that communicate with the tester automatically create a timestamped
CSV session in `~/ebc-logs` (or the directory supplied with `--log-dir`). Use
`--no-log` only when a session log is not wanted. Each status row contains both
decoded values and the raw response frame, and rows are flushed as they are
written so a live test remains useful if it is interrupted.

When an active command finishes, it prints the stop reason, elapsed host time,
sample count, and the last decoded tester status when one was received.
Active sessions also calculate independent host-side Ah and Wh totals using
trapezoidal integration of the sampled current and voltage. Intervals longer
than 30 seconds are skipped and called out in the summary rather than guessed.

The existing GUI must be closed before using a serial command: two programs
must not read from the tester's one serial port at the same time. Commands that
start a charge, recovery, bulk-to-float, or capacity test require both
`--execute` and `--i-am-present` as an explicit confirmation; `stop` requires
`--execute`.
Normal CLI commands take an advisory lock so two CLI processes cannot operate
the same serial device concurrently. `stop --execute` deliberately bypasses
that lock so it remains an emergency path. The plan command is read-only and
never transmits.

## Installation

### From a checkout

Install [UV](https://docs.astral.sh/uv/) and Python 3.14 or newer, then run:

```bash
git clone https://github.com/OWNER/REPOSITORY.git
cd REPOSITORY
uv tool install .
ebc --help
```

Replace `OWNER/REPOSITORY` with the GitHub location of your checkout. To keep
the command tied to a working tree during development, use `uv run` instead:

```bash
uv sync
uv run ebc --help
```

### Directly from GitHub

After the project is published, UV can install the CLI directly from Git:

```bash
uv tool install 'git+https://github.com/OWNER/REPOSITORY.git'
```

### Serial permissions

On Linux, identify the adapter and make sure your user can open it:

```bash
ls -l /dev/ttyUSB*
```

The device is commonly owned by the `uucp` or `dialout` group, depending on
the distribution. Add your user to the group shown by `ls -l`, then start a
new login session (or run the command from a shell started with `newgrp`).

The default device is `/dev/ttyUSB0`; override it with `--device`.

## Usage

Read one measurement:

```bash
ebc status
```

Watch live measurements and retain three samples:

```bash
ebc monitor --count 3 --interval 2
```

List the source-defined operating modes and their frame opcodes:

```bash
ebc modes
```

This is an inventory only. The displayed start/continue/adjust values are
source templates; no parameterized frame is built or transmitted. Existing
A10H PbAc, CCCV, and constant-current profiles are validated by their normal
supervised commands; the other listed modes remain research-only. The full
source mapping and packet formulas are in
[docs/ebc-controller-operating-modes.md](docs/ebc-controller-operating-modes.md).

Build a packet for offline inspection (this also never connects):

```bash
ebc plan-mode c_NiMH --current 0.75 --cells 4 --timeout-minutes 180
ebc plan-mode d_CP --power 10 --cutoff-voltage 10.5 --timeout-minutes 60
```

Inspect a charge packet without sending it:

```bash
ebc plan-lead-acid-charge --current 0.5
```

Start a supervised float charge (bounded to 13.7 V and 0.5 A by default):

```bash
ebc float-charge --execute --i-am-present
```

Start a normal supervised bulk-to-float charge. The default is 0.8 A PbAc
bulk charging with a 24-hour hard timeout, followed by a 13.7 V / 0.5 A CCCV
float hold for up to four hours:

```bash
ebc bulk-to-float --execute --i-am-present
```

The float phase starts only when the tester reports its PbAc auto-off state;
if bulk charging reaches its timeout first, the tester is stopped and float is
not started. Use `--bulk-current`, `--bulk-timeout-minutes`, `--float-current`,
`--float-voltage`, or `--float-timeout-minutes` to review and adjust the
bounded stages. Both stages and the transition are recorded in the session CSV.

Start the standard low-rate 12 V capacity test. The default profile is 0.8 A
constant-current discharge to 10.5 V with a 24-hour hard timeout:

```bash
ebc capacity-test --execute --i-am-present
```

The capacity command owns its live logging; no separate monitor process is
needed. To use a different profile, set `--current`, `--cutoff-voltage`, or
`--timeout-minutes`. The CSV path is printed when the session starts.

Stop the tester:

```bash
ebc stop --execute
```

Summarize a completed session without hardware:

```bash
ebc report ~/ebc-logs/20260903-223424_capacity-test.csv
```

Run the test suite from a checkout with:

```bash
uv run python -m unittest discover -s tests -v
```

Run linting and formatting checks with:

```bash
uv run ruff check .
uv run ruff format --check .
```

## Protocol notes

The implementation validates the EBC XOR checksum, including the firmware's
`0xF?` to `0x0?` framing escape. This is stricter than the older GUI's
default **Ignore CRC** setting. Raw frames are retained in the CSV to make
future decoder improvements possible without repeating a test.

The CSV column `session_elapsed_s` is the CLI's monotonic elapsed time for
active sessions. The separate `device_timer_min` column is the timer field
reported by the tester; it is not used as the session clock because some
firmware responses report a non-wall-clock timer value.

The model registry is derived from the `Models` sections in the
[EBC-controller `ebcc.init`](https://github.com/JOGAsoft/EBC-controller/blob/main/ebcc.init):

| Model | ID | Current factor | Voltage factor | Control |
| --- | --- | ---: | ---: | --- |
| EBC-A05 | `0x05` | `10` | `1` | read-only pending capture |
| EBC-A10H | `0x06` | `1` | `1` | validated |

Both entries use the source's `FA 05 ... F8` connect, `FA 02 ... F8` stop,
and `FA 06 ... F8` disconnect frames. The Lazarus form resource says 8O1,
while the working A10H connection uses 8E1; the registry keeps the validated
8E1 settings until captures resolve whether that discrepancy is family-wide.
The GUI's unknown-device fallback copies the first model's A05 scaling; this
CLI deliberately keeps unknown IDs at neutral scaling and read-only status so
an unidentified tester cannot be silently mis-scaled or controlled.
Unknown model IDs remain readable for diagnostics, but control profiles reject
them until their serial settings and packet behavior have been verified.

### CSV schema

The current CSV schema is version `1`. A newly created file keeps the stable
11-column header shown below, followed by one metadata row whose `event` is
`schema-v1`; all other fields in that row are empty:

```text
timestamp,event,session_elapsed_s,voltage_v,current_a,power_w,capacity_ah,device_timer_min,model_id,model_name,raw_frame_hex
2026-09-03T20:00:00-04:00,schema-v1,,,,,,,,,
```

Status rows have `event=status` or a profile-specific sample event and include
`raw_frame_hex`. Command lifecycle rows have an event name and blank
measurement fields. Readers should ignore metadata and lifecycle rows when
building a measurement series. Files created before schema markers were added
have the same header and are treated as legacy schema `0`; the logger appends
to them without rewriting or adding a marker.

## License

This project is released under the MIT License; see [LICENSE](LICENSE).

See [ROADMAP.md](ROADMAP.md) for planned protocol, measurement, and CLI
features.
