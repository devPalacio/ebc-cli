# A10H mode checks

These are short, sanitized checks against an AC-powered EBC-A10H on
2026-09-05. The connected battery was at approximately 12.9 V. Each attempt
was stopped explicitly; the final idle reading was 12.891 V at 0.000 A.

The captures confirm that the source-defined packet shapes were transmitted
and that the tester returned checksum-valid status frames afterward. They are
short smoke checks rather than sustained-load verification; the source
implementation remains the control basis for the CLI mode path.

## Constant power (`d_CP`)

The 1 W start packet was:

```text
FA 11 00 01 04 5A 00 01 4F F8
```

The tester returned valid status frames including:

```text
FA 00 00 00 35 AF 00 00 00 00 00 0C 04 5A 00 01 06 CF F8
FA 6F 00 00 35 AF 00 00 00 00 01 3E 0C 63 04 47 06 E0 F8
```

The decoded current remained 0.000 A during the five-second observation.

## Software constant resistance (`d_CR`)

The 100 ohm start packet, calculated from a 12.8 V observation, was:

```text
FA 01 00 0C 04 5A 00 01 52 F8
```

The corresponding adjustment packet for a 12.7 V observation was:

```text
FA 07 00 0C 04 5A 00 01 54 F8
```

The tester returned valid frames with states `0x01` and `0x6E`, but the
decoded current remained 0.000 A during the three-second observation.

## Source cross-reference

The local EBC-controller implementation agrees with these packet formulas:

- `d_CP` uses opcode `0x11`, source power encoding, cutoff voltage, and the
  device timer;
- `d_CR` reuses the CC start/adjust opcodes `0x01` and `0x07`, computes
  current as the latest measured voltage divided by resistance, and sends an
  adjustment every third valid sample;
- STOP is the common `FA 02 ... F8` frame and is sent when the source breaks a
  run.

Source agreement checks the packet formulas, not whether the tester accepted
the setpoint or applied a load. Both observations recorded zero current.
Sustained CP regulation, CR feedback under load, and the corresponding cutoff
behavior remain unverified. A checksum-valid reply alone is not an acknowledgement
of a start command. The CLI still enables these source-defined A10H modes;
that enablement must not be read as hardware validation.
