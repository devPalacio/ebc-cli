# Command reference

Use `ebc COMMAND --help` for the full option list. Serial commands accept
`--device`, `--timeout`, `--log-dir`, and `--no-log`. `doctor` accepts only
`--device` and does not open the port.

## Observation and offline commands

| Command | Behavior |
| --- | --- |
| `doctor` | Inspect serial permissions and visible locks. No serial I/O. |
| `status` | Send CONNECT, discard its initial reading, and require a follow-up status. No mode change. |
| `monitor` | Poll initially, then read the latest stream sample, with a CONNECT fallback. No mode change. |
| `modes` or `list-modes` | List the controller-derived catalog. No connection. |
| `plan-mode` | Construct a source-format packet. No connection or transmission. |
| `plan-lead-acid-charge` | Construct a PbAc packet with A10H setting checks. No connection. |
| `report PATH` | Decode and summarize a CLI CSV log. No connection. |

`plan-mode` checks the packet format, not the complete live A10H setting
envelope. A plan that prints successfully is not approval to transmit it.
Live commands reject settings outside the A10H envelope and reject irrelevant
mode options rather than silently ignoring them.

`status` requires a streamed follow-up. Poll-only firmware can therefore time
out even if CONNECT returns a valid initial frame. `monitor` can use that
initial frame, which may be cached.

## Supervised profiles

Every live start requires `--execute --i-am-present`. `charge` and
`capacity-test` require explicit battery settings; neither infers chemistry.
Tester limits are not battery recommendations.

| Command | Required settings | Completion |
| --- | --- | --- |
| `charge` | `--current`, `--voltage`, `--end-current`, `--timeout-minutes` | Host end-current dwell, observed device auto-off, or timeout |
| `capacity-test` | `--current`, `--cutoff-voltage`, `--timeout-minutes` | Device CC cutoff or timeout |

`capacity-test` no longer defaults to a 12 V lead-acid discharge. Existing
scripts must supply all three settings. It accepts single-cell voltages and
other settings within the tester envelope. Capacity settings whose current
multiplied by cutoff exceeds the load power ceiling are rejected before I/O.
CC discharge also checks current times observed baseline voltage before START.
These guards do not continuously regulate power or replace hardware protection.
Charge throughput is not discharge capacity. Charge and discharge remain
separate operations and logs.

Capacity results distinguish measurement validity from the battery's capacity:

- **Completed:** fresh post-start discharge activity was observed, followed by
  the device cutoff, without a detected measurement-quality problem.
- **Incomplete:** the endpoint was not established, the operation was interrupted,
  or no discharge activity was observed. A cutoff-only sample is not a completed test.
- **Invalid:** observation coverage or measured load current compromised the test.

Partial Ah and Wh remain available, but do not represent a verified full-capacity
measurement. No rated-capacity percentage or battery pass/fail decision is made.
Capacity sampling requires streamed observations. A failed streamed read attempts
STOP rather than accepting a potentially cached CONNECT reply as fresh evidence.
The current-quality tolerance is `max(0.02 A, 5% of requested current)`, inclusive.
This is an engineering screen, not a claim of calibrated accuracy. A zero-current
terminal cutoff is not a load-current mismatch. Initial or trailing unobserved
time over 30 seconds, skipped integration gaps, and non-increasing observation
timestamps invalidate an otherwise observed discharge measurement. Shorter
unobserved start and tail periods are disclosed, not extrapolated.

`ebc report PATH` recomputes the same assessment from saved request metadata,
ordered observations, and termination evidence. Older or ambiguous logs are
labelled **unverified**, never assumed completed. Interrupted runs retain partial
samples when logging works. `--no-log` disables durable replay, but the CLI still
prints the in-memory partial assessment on ordinary read errors or interruption.
Contradictory cutoff readings invalidate the measurement: terminal current may
fall toward zero, but cannot exceed the requested load plus its current tolerance.
The endpoint-voltage screen allows rebound of `max(0.10 V, 5% of cutoff)` above
the requested cutoff. Larger discrepancies are flagged, not treated as evidence
that the requested endpoint was reached. This allowance is an engineering
consistency check, not a battery limit or a measured firmware characteristic.
A capacity cutoff received before its deadline remains eligible for completion
even if output runs past the deadline; receipt at or after the deadline does not.

`charge` sends the existing CCCV start packet. Its end-current threshold is
host policy, not a transmitted firmware setting. The pre-start baseline does
not prove charging began. After observed charging, the host requires positive
current at or below `--end-current` and voltage within `--voltage-tolerance`
of the target for `--end-current-dwell-seconds` continuously. Defaults are
0.05 V tolerance, 30 seconds dwell, and 5 seconds maximum observation gap
(`--max-sample-gap-seconds`). A current rebound, voltage departure, zero
current, unexpected state, or excessive gap resets the dwell. Charge sampling
requires streamed observations; potentially cached CONNECT replies cannot prove
activity or completion. A failed streamed read aborts the charge and attempts
STOP rather than falling back to CONNECT. Dwell and gap checks use receipt times before output
and logging, and an expired deadline takes precedence over completion. These are
sampling-policy defaults, not manufacturer limits or calibrated accuracy.

Device CCCV auto-off is reported separately from host end-current completion.
The CLI does not select the firmware's internal cutoff current. No built-in
thermal interlock is added. Read the cell's charge and test requirements before
choosing settings; an 18650 size alone does not establish them.

The following defaults remain specific to six-cell, nominal 12 V lead-acid
batteries. They are not recommendations for an unknown battery.

| Command | Default settings | Host timeout |
| --- | --- | --- |
| `recover-lead-acid` | PbAc, 0.20 A | 30 minutes |
| `float-charge` | CCCV, 13.7 V, 0.50 A | 120 minutes |
| `bulk-to-float` | PbAc at 0.80 A, then CCCV at 13.7 V and 0.50 A | 1440 minutes bulk, 240 minutes float |

`recover-lead-acid` is a low-current PbAc attempt, not desulfation or a
promise to recover a damaged battery. Float charging is a bounded hold, not
an unattended maintenance service.

`bulk-to-float` requires a positive-current bulk sample without a known
completion code, then PbAc auto-off `0x1A`, before the bulk deadline.
Otherwise it does not start float. `capacity-test` ends on CC cutoff `0x14`
or timeout. Recovery and float commands remain bounded by their host timeout;
they do not use the generic mode completion policy.

Named profiles produce the same `EbcModeCommand` used by `run-mode`. They
share its packet encoder and model-aware live validation, but retain their
own battery limits, phase transitions, and completion policies.
See the [agent guide](../skills/create-charge-profile/SKILL.md) for adding a
recipe without adding an encoder. Profile creation is a code or planning
task, not permission to start a charge.

The same profiles are callable from Python; see [library use](library.md).

## Source-defined modes

`run-mode` uses a 60-minute host timeout by default. Its device timer uses
the same requested minutes, except that exactly 250 encodes as 249 to match
the controller workaround. Live timeout values are 1 through 61439 minutes.

```bash
ebc run-mode c_Pb --current 0.80 --cells 6 --timeout-minutes 60 \
  --execute --i-am-present
ebc run-mode c_CCCV --current 0.50 --voltage 13.7 --timeout-minutes 120 \
  --execute --i-am-present
ebc run-mode d_CC --current 0.80 --cutoff-voltage 10.5 --timeout-minutes 60 \
  --execute --i-am-present
```

Cell-charge modes take `--current` and `--cells`. CCCV takes `--current` and
`--voltage`. CC discharge takes `--current` and `--cutoff-voltage`.
CP takes `--power` and `--cutoff-voltage`; software CR takes `--resistance`
and `--cutoff-voltage`. There is no live Continue command or step-program
runner. `plan-mode --phase cont` and `--phase adjust` inspect those packets
offline where a mode defines them.

CP retains the source's unusual encoding. Within the 150 W product envelope,
requests above 120 W fail byte validation. Live CP requires whole watts from
1 through 120, rather than silently truncating a fractional request.
Offline plans retain the source truncation, so a sub-watt plan encodes zero.
Neither packet planning nor a valid status response demonstrates load regulation.

Software CR calculates `I = V / R` from the baseline and every third host
sample. Current is truncated to the representable wire setting before the
resolution check, while out-of-range calculated currents are rejected.
It is a sampled CC adjustment loop, not hardware CR. Zero current after the
first three samples or the mode's auto-off byte ends a generic mode session.
See [capture limitations](a10h-mode-captures.md).

## STOP and failure limits

`ebc stop --execute` sends the common STOP packet without model discovery,
status reads, or the advisory CLI lock. It attempts logging only afterward.
A successful write is not a hardware acknowledgement. Opening a second port
can interfere with another reader, and advisory locks do not exclude the GUI.

Supervised sessions attempt STOP on timeout, completion, Ctrl-C, SIGINT,
SIGTERM, SIGHUP, and ordinary errors. SIGKILL, a hung process, failed serial
writes, or lost host power can prevent cleanup. Output and log writes are
synchronous, so a host deadline is not a real-time hardware guarantee.

## CSV logs

Serial commands create unique, flushed logs under `~/ebc-logs`. The header is
stable, followed by a `schema-v1` event. Older logs with that header and no
schema marker are treated as version 0.

```text
timestamp,event,session_elapsed_s,voltage_v,current_a,power_w,capacity_ah,device_timer_min,model_id,model_name,raw_frame_hex
```

Measurement rows include raw status frames. Lifecycle and schema rows have
blank measurement fields. Replay decodes the raw frame rather than trusting
the displayed numeric columns. Active samples use monotonic host elapsed
time; the firmware timer is telemetry only. Trapezoidal Ah and Wh integration
skips gaps longer than 30 seconds and does not estimate the missing energy.
A report's duration ends at the last recorded sample, not necessarily at STOP.
