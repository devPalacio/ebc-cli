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

## Quickstart

These are software examples. Use the values documented for your battery. A cell
size or a nominal voltage alone does not establish safe current, endpoint, or
temperature limits.

Charge a 4.2 V Li-ion 18650. The values below are the standard charge for a
Samsung INR18650-30Q: 1.5 A to 4.2 V, 150 mA cut-off.

```bash
ebc charge --current 1.5 --voltage 4.2 --end-current 0.15 --timeout-minutes 180 \
  --execute --i-am-present
```

Float-charge a 12 V lead-acid battery: CCCV hold at 13.7 V and 0.5 A. For a
deeply discharged battery, `bulk-to-float` runs a PbAc bulk phase first.

```bash
ebc float-charge --current 0.5 --voltage 13.7 --timeout-minutes 120 \
  --execute --i-am-present
```

Check capacity by discharging to the documented endpoint. The first command is
the INR18650-30Q standard discharge (0.6 A to 2.5 V); the second is a 12 V
lead-acid example.

```bash
ebc capacity-test --current 0.6 --cutoff-voltage 2.5 --timeout-minutes 360 \
  --execute --i-am-present
```

```bash
ebc capacity-test --current 0.8 --cutoff-voltage 10.5 --timeout-minutes 240 \
  --execute --i-am-present
```

Charge and capacity testing are separate runs with separate logs. There is no
built-in charge-rest-discharge sequence. The result reports whether the
discharge was actually observed, so a cutoff alone is not a completed test.

## Use

```bash
ebc status
ebc monitor --count 3 --interval 2
ebc modes
ebc plan-mode c_Pb --current 0.5 --cells 6 --timeout-minutes 60
```

Other profile commands are `recover-lead-acid` and `bulk-to-float`. `run-mode`
exposes the source-defined device modes;
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
