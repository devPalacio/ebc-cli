# ebc-cli

A Linux CLI for ZKETECH EBC battery testers. Read measurements, plan packets,
run supervised charge and discharge profiles, and record CSV logs.

The control path targets the **AC-powered EBC-A10H** at 9600 baud, 8E1.
EBC-A05 measurements use source-documented scaling, but A05 control is disabled.
Other model IDs are diagnostic-only. This is not a full port of the
[EBC-controller GUI](https://github.com/JOGAsoft/EBC-controller).

**Use at your own risk and stay with the battery.** Host timeouts and STOP
are best-effort, not a safety system.

## Install

Requires Linux, Python 3.14 or newer, and [uv](https://docs.astral.sh/uv/).
From a checkout of this repository:

```bash
uv tool install .
ebc --help
```

Your account needs read/write access to the serial adapter. Close the GUI
before using the CLI. The default device is `/dev/ttyUSB0`; set `--device`
on the command to use another adapter. `ebc doctor` checks permissions and
visible locks without opening the port.

## Use

```bash
ebc status
ebc monitor --count 3 --interval 2
ebc modes
ebc plan-mode c_Pb --current 0.5 --cells 6 --timeout-minutes 60
```

Live starts require both `--execute` and `--i-am-present`.

```bash
ebc capacity-test --current 0.8 --cutoff-voltage 10.5 --timeout-minutes 120 \
  --execute --i-am-present
ebc stop --execute
```

Other profile commands are `recover-lead-acid`, `float-charge`, and
`bulk-to-float`. `run-mode` exposes the source-defined device modes;
See [usage and defaults](docs/usage.md)
and [capture evidence](docs/a10h-mode-captures.md) before live use.

Serial commands log to `~/ebc-logs`. Use `--log-dir` or `--no-log` to change
that.

```bash
ebc report path/to/session.csv
```

## Development and reference

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv build
```

- [Library use: embed a profile run in Python](docs/library.md)
- [Create a charge profile: agent instructions](skills/create-charge-profile/SKILL.md)
- [Controller comparison](docs/controller-review.md)
- [State diagrams](docs/state-diagrams.md)
- [Protocol and operating modes](docs/ebc-controller-operating-modes.md)
- [A10H operating limits](docs/ebc-a10h-operating-limits.md)
- [Source provenance](PROVENANCE.md)

## License

[GNU GPL version 3 only](LICENSE) (`GPL-3.0-only`), provided **as is**,
without warranty or a support commitment. A Python CLI port of
[EBC-controller](https://github.com/JOGAsoft/EBC-controller).
See [NOTICE](NOTICE) for attribution and [PROVENANCE.md](PROVENANCE.md)
for the source reference.
