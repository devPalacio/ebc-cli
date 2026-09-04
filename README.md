# ebc-cli

`ebc-cli` is a small terminal interface for ZKETECH EBC electronic loads and
chargers.  Its protocol layer is derived from the locally installed
EBC-controller source, which identifies the EBC-A10H as model id `0x06`.

## Current scope

- `status` asks the tester for one status frame at 9600 8E1.
- `monitor` prints status frames until interrupted.
- `plan-lead-acid-charge` prints, but **does not transmit**, the packet the
  tester would receive for a PbAc charge plan.
- `recover-lead-acid` transmits one fixed, explicitly gated recovery profile:
  PbAc, six cells, 14.4 V target, 0.01–0.20 A, 1–30 minute timeout.
- `float-charge` transmits a bounded 12 V lead-acid CCCV float profile.
- `stop --execute` transmits the immediate EBC stop frame.

The existing GUI must be closed before using any serial command; two programs
must not read from the tester's one serial port at the same time. Recovery is
for an inspected, supervised battery only and requires both `--execute` and
`--i-am-present`.

The protocol code validates the EBC's XOR checksum, including the firmware's
`0xF? → 0x0?` framing escape. This is more precise than the older GUI's
default **Ignore CRC** checkbox.

## Development

Run the test suite with UV:

```bash
cd ~/src/ebc-cli
uv run python -m unittest discover -s tests -v
```

Show the CLI help:

```bash
ebc --help
```

The `ebc` launcher invokes this local project through UV. It never starts a
charge in this initial release.

Every command that communicates with the tester automatically creates a
timestamped CSV in `~/ebc-logs`. Disable that only with `--no-log`.

Record a bounded live-monitor session:

```bash
ebc monitor --count 3 --interval 2
```

The logger flushes every sample and records both interpreted values and the
raw response frame.

Run a standard low-rate 12 V capacity test. This command remains open, logs
every received status sample, and sends a stop frame when the tester reports
its CC cutoff:

```bash
ebc capacity-test --execute --i-am-present
```

If a shell has not picked up the `uucp` group, serial access to `/dev/ttyUSB0`
will fail. Start a fresh login session after adding the group, or run a shell
with `newgrp uucp` and invoke the CLI from there.
