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

## Charge and capacity-test profiles

Both operations use `run_profile`. Construct profiles offline with explicit
battery-specific settings:

```python
from ebc_cli import make_charge_profile, make_capacity_test_profile

# Software examples, not settings for an unidentified cell.
charge = make_charge_profile(
    current_amps=1.5,
    voltage_volts=4.2,
    end_current_amps=0.15,
    timeout_minutes=180,
)
discharge = make_capacity_test_profile(
    current_amps=0.6,
    cutoff_volts=2.5,
    timeout_minutes=360,
)
```

Creating either object performs no I/O. Run each separately only after
confirming the battery's requirements and setup. There is no automatic
charge-rest-discharge sequence.

Charge termination uses host observations, not an extra wire field. Optional
`end_current_dwell_seconds=30`, `voltage_tolerance_volts=0.05`, and
`max_sample_gap_seconds=5` set the observation policy. Charging must be observed
in a post-start active sample. Positive current at or below the end-current
threshold must then remain within the voltage window for the dwell. Rebounds,
voltage departures, zero current, unexpected states, and observation gaps reset
the dwell. Each call creates fresh completion state. Only streamed post-start
observations qualify, not potentially cached CONNECT responses. Dwell timing
uses receipt timestamps before synchronous output and logging. If output runs
past the deadline, timeout takes precedence over completion.

`SessionStopReason.END_CURRENT` records host termination.
`SessionStopReason.MODE_AUTO_OFF` records observed device auto-off after
charging activity, without claiming the firmware met the requested end current.
`SessionStopReason.TIMEOUT` does not mean a completed charge or capacity test.
The runner still attempts STOP on every normal completion or error.

Capacity testing accepts the tester's setting range rather than a 12 V
lead-acid-only range. It ends on device CC cutoff or timeout and retains the
existing discharge accounting and logs. Requests above the load power ceiling
at the cutoff are rejected before I/O. The device layer also checks CC current
times baseline voltage against its registered model power limit before START.
Capacity completion requires fresh post-start discharge observations before the
cutoff. A baseline, cached CONNECT reply, or cutoff-only zero-current sample
cannot establish completion. The result distinguishes completed, incomplete,
and invalid measurements and retains partial Ah and Wh. A completed test is
not a battery pass/fail decision.

The reported host integral skips observation gaps over 30 seconds. A gap that
compromises coverage invalidates the capacity measurement instead of silently
presenting the smaller integral as full capacity. Requested current is compared
with observed discharge current; terminal cutoff samples are not mistaken for
load-regulation failures. These are software quality checks, not a calibration
guarantee.

Capacity runs expose `run.result.capacity`, a `CapacityAssessment` containing
`outcome`, `reasons`, `measurement`, `discharge_observed`, `termination`,
`elapsed_seconds`, `request`, and `unobserved_seconds`. Outcomes are
`CapacityOutcome.COMPLETED`, `INCOMPLETE`, `INVALID`, or `UNVERIFIED` for logs
without adequate evidence. `SessionReport.capacity` uses the same type and
assessment policy. The current screen allows `max(0.02 A, 5% of requested
current)` error, inclusively. Duplicate or backward observation times cannot
support a completed measurement.

Exceptions still propagate and trigger STOP cleanup. When logging is enabled,
accepted samples are flushed before user callbacks so an interrupt does not
discard the last observation. Partial results can be reconstructed with
`ebc report PATH`; legacy or ambiguous logs are explicitly unverified. Request
and assessment metadata use event rows without changing the CSV columns.
No durable replay is available with `--no-log` or a failed log write. Exceptional
capacity exits also attach the in-memory partial assessment to the original
exception as `exc.capacity_assessment`, independent of logging. The original
exception type and CLI exit behavior remain unchanged.

A cutoff received before the capacity deadline remains eligible even when
subsequent output delays STOP past that deadline. The endpoint screen allows
`max(0.10 V, 5% of requested cutoff)` rebound above cutoff; larger discrepancies,
wrong-mode observations, and terminal current above the requested load plus its
tolerance invalidate the measurement. Zero or declining current at cutoff is
allowed. These are engineering consistency checks, not calibrated endpoint
accuracy or an aircraft maintenance acceptance standard.

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
