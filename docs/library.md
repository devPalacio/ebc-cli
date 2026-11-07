# Library use

`ebc_cli` is a package first and a CLI second. The commands are thin handlers
around these objects, so a script gets the same packets, logging, and
stop-on-exit behavior as the CLI.

## Run a validated profile

```python
from ebc_cli import EbcDevice, StatusCsvLogger, make_float_charge_profile, run_profile

profile = make_float_charge_profile(current_amps=0.5, voltage_volts=13.7, timeout_minutes=120)

with EbcDevice("/dev/ttyUSB0") as device, StatusCsvLogger("float.csv") as logger:
    run = run_profile(device, profile, logger=logger, on_sample=lambda status: print(status))
    print(run.result.stop_reason, run.result.samples, run.result.measurement.amp_hours)
```

`run_profile` starts the profile inside a `SupervisedSession`, so STOP is
attempted whenever the call exits: normal completion, cutoff, timeout, signal,
or an exception. It returns a `ProfileRun` with the start `baseline` and the
`SessionResult` (stop reason, sample count, elapsed time, final status, host
integration).

Reset your own clock for testing; the loop stays the same:

```python
now = [0.0]
run_profile(device, profile, clock=lambda: now[0], sleeper=lambda s: now.__setitem__(0, now[0] + s))
```

`on_sample` is optional. Without it the function writes nothing to stdout, so
it is safe inside a service or notebook.

## Two-phase lead-acid charging

```python
from ebc_cli import make_bulk_to_float_profile, run_bulk_to_float

profile = make_bulk_to_float_profile(
    bulk_current_amps=0.8,
    bulk_timeout_minutes=1440,
    float_current_amps=0.5,
    float_voltage_volts=13.7,
    float_timeout_minutes=240,
)
run = run_bulk_to_float(device, profile, on_float_start=notify)
```

The float phase starts only after observed positive bulk current followed by
the source-defined auto-off state, before the bulk deadline. A bulk timeout
stops the tester and never starts float.

## Packets without hardware

```python
from ebc_cli import A10H_CAPABILITIES, build_mode_packet, make_float_charge_profile

command = make_float_charge_profile(
    current_amps=0.5, voltage_volts=13.7, timeout_minutes=120
).start_command
packet = build_mode_packet(command, capabilities=A10H_CAPABILITIES)  # FA71003205AA007894F8
```

Planning builders are pure byte assembly. Passing `capabilities` applies the
model's electrical envelope; omitting it, as `plan-mode` does, checks framing
and scaling only.

## What the library does not do for you

- **The CLI's confirmation flags are not an interlock.** `--execute` and
  `--i-am-present` exist to stop a human from firing a command by accident.
  A library caller has no such prompt, and `run_profile` does not add one.
- **Starting a mode directly on `EbcDevice` sends no STOP on failure.**
  `device.begin_mode(...)` writes the start frame and returns; if your code
  raises afterwards, the tester keeps running until its own timer or you call
  `device.stop()`. Use `run_profile`/`run_bulk_to_float`, or a
  `SupervisedSession` you manage yourself.
- **Deadlines and STOP are best-effort.** They cannot clean up after SIGKILL,
  a hung host, or lost power.
- **Only A10H live control is validated.** A05 and unknown models stay
  read-only; `begin_mode` raises rather than guessing at an envelope.
- **No profile files.** Profiles are Python calls. There is no JSON/YAML
  preset loader to parse.

## License

The package is GPL-3.0-only. Importing it into another program makes that
program a derivative work for distribution purposes; see `LICENSE` and
`NOTICE`.
