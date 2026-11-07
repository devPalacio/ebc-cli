# CLI and EBC-controller

The CLI is a Python port based on
[JOGAsoft/EBC-controller at `ae13528`](https://github.com/JOGAsoft/EBC-controller/tree/ae13528fe1724ef6825cf6608bb2a033cb1a45a6).
This comparison describes the shared protocol and the differences in control,
logging, and host supervision. It does not certify battery safety or firmware.

## Conclusion

The CLI is a smaller, host-supervised interface to the same protocol, not a
feature-for-feature GUI port. Model IDs, scaling factors, connection frames,
and configured mode opcodes agree. The useful differences are explicit model
control gates, strict frame checks, bounded reads, and independent measurement
accounting. Those protections are not proof that every enabled mode works
under load.

The CLI is distributed as a GPLv3 port, provided as is without warranty or a
support commitment. See [NOTICE](../NOTICE) for attribution and
[PROVENANCE.md](../PROVENANCE.md) for the upstream source reference.

## Behavior comparison

Controller references below use the pinned revision above. CLI links point
to the working source so that they follow later fixes.

| Area | Controller behavior | CLI behavior and consequence |
| --- | --- | --- |
| Models | `ebcc.init:143–161` defines A05 `0x05`, current factor 10, and A10H `0x06`, factor 1. | [protocol.py](../src/ebc_cli/protocol.py) matches. A05 is read-only. Unknown IDs use neutral scaling and cannot start a profile. |
| Unknown model | `main.pas:1028–1030,1187–1204` copies the first model as fallback. | The CLI does not silently inherit A05 scaling or enable unknown hardware. Unknown readings remain diagnostic. |
| Connection | `main.pas:1927–1960` opens, sends STOP and CONNECT; disconnect sends DISCONNECT. | [device.py](../src/ebc_cli/device.py) identifies first with CONNECT, then permits STOP and start preparation only for the configured controllable model. Read-only commands do not send STOP. Close does not send DISCONNECT. |
| Serial parity | `main.lfm:730–738` specifies 9600 8O1. | CLI defaults to 9600 8E1, the configuration used for its recorded A10H work. Neither the CSV frames nor PTYs establish physical parity. The discrepancy remains unresolved; do not generalize to A05. |
| Frame shape | Ten-byte commands and nineteen-byte status frames. `MakePacket2` fills parameters, then `SendData` writes the checksum (`main.pas:772–783,1038–1117`). | [protocol.py](../src/ebc_cli/protocol.py) validates prefix, suffix, length, and XOR. The commented checksum assignment in `MakePacket2` is not a missing outgoing check: `SendData` computes it. |
| CRC policy | Ignore CRC is checked by default (`main.lfm:147–155`); `InterpretPackage` accepts that bypass (`main.pas:607`). | Strict status checks, including the recorded `0xF?` checksum remap to `0x0?`. Corrupt frames are skipped while searching for a valid frame. |
| Modes | `ebcc.init:1–88` contains nine device entries and two host controls. | [modes.py](../src/ebc_cli/modes.py) matches the wire metadata. `plan-mode` never connects. `run-mode` is enabled for A10H, which is not the same as hardware validation of every chemistry. |
| Encoding | Packed current/voltage, cell count for chemistry modes, special CP encoding, and 250-to-249 timer workaround. | Same layouts, plus numeric and byte-range rejection. Decimal-intent conversion avoids Python float underencoding of exact centisteps. CP live requests are restricted to whole watts and the representable range within the product envelope. |
| Software CR | `main.pas:1102–1106,713–716` computes U/R and adjusts every third valid source sample. | The CLI adjusts every third host-observed sample, not every firmware frame. It truncates current to a wire setting before checking resolution and rejects calculated currents outside the model envelope. Sampling cadence can differ from the GUI. |
| Generic completion | `main.pas:718–722` checks the selected auto-off byte or zero current after three samples. | [commands.py](../src/ebc_cli/commands.py) uses that policy for `run-mode`. Source `AutoOff=FF` is retained for CR, not established as a firmware-specific completion event. |
| Profiles | GUI selection and step program configure operations. | [profiles.py](../src/ebc_cli/profiles.py) adds bounded 12 V lead-acid presets. Capacity mode checks CC cutoff. Recovery and float commands use host timeout, not generic zero-current completion. |
| Bulk-to-float | The GUI has a general step engine rather than this named profile. | [session.py](../src/ebc_cli/session.py) requires observed bulk activity, then PbAc auto-off before the deadline, to start bounded CCCV float. A timeout does not transition to float. |
| Host programs | `stepform.pas:162–319` and `main.pas:1745–1924` implement charge/discharge, WAIT, LOOP, and END. | No general step interpreter, cycling, or live Continue interface. Wait and Loop are reference entries only. |
| Host cutoffs | Source charge current/dwell and capacity checks are active (`main.pas:724–751`). Some help-text promises are broader than the runtime. | No GUI-style configurable current-dwell, capacity, or energy cutoffs. Do not claim full step-language parity. |
| Ah and Wh | `main.pas:613,628–631` integrates each latest value over wall-clock sample intervals. | [measurement.py](../src/ebc_cli/measurement.py) uses trapezoids and monotonic active-session timestamps. It skips gaps over 30 seconds rather than inventing missing charge or energy. |
| Logging | GUI plots and its own simpler CSV representation. | [csvlog.py](../src/ebc_cli/csvlog.py) flushes decoded values and raw frames with schema metadata. [report.py](../src/ebc_cli/report.py) re-decodes raw frames. Formats and integration totals need not match the GUI. |
| Ownership and STOP | GUI owns its connection. | Advisory CLI lock, plus an explicit unlocked STOP path. This does not prevent a non-cooperating GUI from using the same port. STOP delivery and hardware execution are not confirmed. |

## Remaining limits

1. **CP/CR regulation is unverified.** The existing short captures recorded
   zero current. They establish transmitted bytes and valid replies, not
   setpoint acceptance, sustained regulation, or cutoff behavior.
2. **Parity is unresolved.** Retaining the working 8E1 configuration is not a
   finding that the GUI's 8O1 resource is wrong. A hardware-level capture is
   needed to settle that difference.
3. **Host supervision has limits.** USB loss, SIGKILL, host power loss, or
   blocked synchronous I/O can defeat the deadline or STOP path. Opening the
   port and writing STOP do not acknowledge an idle tester.
4. **Setting checks are not battery protection.** The current/voltage ranges
   do not validate chemistry, temperature, supply margin, or a battery's safe
   operating area. CC and CR do not dynamically enforce V times I against
   the advertised 150 W rating.
5. **Measurement summaries have boundaries.** Integration starts at the first
   active sample, omits gaps, and sums current magnitudes across phases. A
   combined charge/discharge or bulk/float report is not a net state-of-charge
   estimate. Duration is the last sample time, not a verified stop time.

## Reproduce the checks

```bash
uv sync --locked
uv run python tools/compare_controller.py ../EBC-controller/ebcc.init
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv build
uv run ebc modes
uv run ebc plan-mode d_CP --power 10 --cutoff-voltage 10.5
uv run ebc report tests/fixtures/a10h_session.csv
```

The comparison script checks configuration metadata, not Pascal execution.
Its controller input is explicit and is not required by the package or CI.
Tests use generated frames, stored observations, mocks, and pseudo-terminals.
They do not prove physical serial parity or battery behavior.
