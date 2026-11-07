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

These defaults target six-cell, nominal 12 V lead-acid batteries. They are
not recommendations for an unknown battery. Every live start requires
`--execute --i-am-present`.

| Command | Default settings | Host timeout |
| --- | --- | --- |
| `recover-lead-acid` | PbAc, 0.20 A | 30 minutes |
| `float-charge` | CCCV, 13.7 V, 0.50 A | 120 minutes |
| `bulk-to-float` | PbAc at 0.80 A, then CCCV at 13.7 V and 0.50 A | 1440 minutes bulk, 240 minutes float |
| `capacity-test` | CC discharge, 0.80 A, 10.5 V cutoff | 1440 minutes |

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
