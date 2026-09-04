# ebc-cli

`ebc-cli` is a small terminal interface for ZKETECH EBC battery testers. It
reads measurements, starts bounded charge/discharge profiles, sends an
immediate stop command, and records status frames as CSV.

The current protocol implementation targets the ZKETECH EBC-A10H (model ID
`0x06`) over a 9600 baud, 8 data bit, even parity, 1 stop bit serial link
(8E1). It was tested with an EBC-A10H connected through a USB serial adapter;
other EBC models are not currently supported or validated. Do not use a
control profile with another model until its protocol has been checked.

## Features

- `status` reads one status frame without changing the tester mode.
- `monitor` streams status frames until interrupted or until `--count` is
  reached.
- `plan-lead-acid-charge` prints a PbAc charge packet without transmitting it.
- `recover-lead-acid` starts a bounded, supervised PbAc recovery profile.
- `float-charge` starts a bounded 12 V lead-acid CCCV float profile.
- `capacity-test` runs a low-rate constant-current discharge and keeps
  recording samples until the tester reaches its cutoff or the safety timeout.
- `stop --execute` sends the immediate EBC stop frame.

Commands that communicate with the tester automatically create a timestamped
CSV session in `~/ebc-logs` (or the directory supplied with `--log-dir`). Use
`--no-log` only when a session log is not wanted. Each status row contains both
decoded values and the raw response frame, and rows are flushed as they are
written so a live test remains useful if it is interrupted.

The existing GUI must be closed before using a serial command: two programs
must not read from the tester's one serial port at the same time. Commands that
start a charge, recovery, or capacity test require both `--execute` and
`--i-am-present` as an explicit confirmation; `stop` requires `--execute`.
The plan command is read-only and never transmits.

## Installation

### From a checkout

Install [UV](https://docs.astral.sh/uv/) and Python 3.11 or newer, then run:

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

Inspect a charge packet without sending it:

```bash
ebc plan-lead-acid-charge --current 0.5
```

Start a supervised float charge (bounded to 13.7 V and 0.5 A by default):

```bash
ebc float-charge --execute --i-am-present
```

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

Run the test suite from a checkout with:

```bash
uv run python -m unittest discover -s tests -v
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

## License

This project is released under the MIT License; see [LICENSE](LICENSE).

See [ROADMAP.md](ROADMAP.md) for planned protocol, measurement, and CLI
features.
