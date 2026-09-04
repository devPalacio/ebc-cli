# Roadmap

This project is a focused, Linux-native command-line control plane for the
ZKETECH EBC-A10H. The roadmap records useful next steps without making the
Python CLI a second copy of the Pascal/Lazarus GUI.

## Current baseline

Completed in the current `0.1.x` baseline:

- A10H protocol framing, scaling, checksum validation, and raw-frame capture.
- Read-only status and monitor commands.
- Gated lead-acid recovery, CCCV float charging, and constant-current
  discharge profiles.
- Automatic flushed CSV session logs, including host monotonic elapsed time.
- Best-effort stop on normal completion, operator interrupt, signal, and
  serial failure paths.
- Final active-session summaries with stop reason, elapsed duration, sample
  count, and the last decoded tester status.
- Typed, validated charge and discharge profile objects shared by the CLI
  handlers.
- Semantic completion reasons for the known A10H capacity cutoff status.
- Advisory CLI serial-device ownership with an emergency-stop bypass.
- Hardware-free protocol, serial, and lifecycle tests, plus UV packaging
  checks.

## Near term: make the control path boring

- [x] Replace raw status-byte checks with semantic tester states and completion
  reasons.
- [x] Introduce typed charge/discharge profile objects so validation and packet
  construction live together instead of in individual CLI branches.
- [x] Enforce one CLI process owning a serial device at a time, while
  preserving an emergency stop path.
- [x] Add pseudo-terminal integration tests for serial framing, partial reads,
  timeouts, disconnects, and stop-on-cleanup behavior.
- [ ] Capture and replay real A10H sessions in tests, including both parity and
  checksum variants encountered in the field.
- [ ] Compute host-side Ah and Wh independently from sampled current, voltage,
  and monotonic elapsed time.

## Measurement and analysis

- [ ] Add a read-only `report` command for summarizing a CSV capacity run.
- [ ] Add a replay adapter so logs can be inspected without the tester
  connected.
- [ ] Define a documented CSV schema/version and a compatibility exporter for
  the original GUI's simpler CSV format.
- [ ] Add optional JSON output for scripts and monitoring systems.

## Protocol and device coverage

- [ ] Move model-specific serial settings, scaling factors, and connection
  packets into a model registry.
- [ ] Validate support for EBC-A05 and document its current scaling before
  enabling control commands for it.
- [ ] Add protocol support for constant-power discharge and the GUI's
  adjustable/continuous commands after capturing representative frames.
- [ ] Decide how to import or replace the GUI's `ebcc.init` packet definitions
  without making the CLI depend on Lazarus files at runtime.
- [ ] Document the observed 8E1/odd-parity discrepancy between the GUI resource
  and the working A10H Python connection, with captured wire evidence.

## Test profiles and automation

- [ ] Add configurable capacity cutoffs by current, capacity, energy, and
  dwell time.
- [ ] Add battery presets that store chemistry, nominal voltage, capacity,
  current, cutoff, and timeout as reviewable files.
- [ ] Add a small step-program format with charge, discharge, wait, and loop
  steps.
- [ ] Support cycle testing with capacity-retention criteria, preserving an
  explicit operator confirmation for any profile that transmits.

## User experience and distribution

- [ ] Add shell completion and a concise `ebc help <command>` workflow.
- [ ] Add a stable release/install path once the GitHub repository and package
  name are settled.
- [ ] Publish example logs and a short protocol-research note explaining what
  is confirmed versus inferred.
- [ ] Consider a terminal dashboard only after the non-interactive commands and
  log/report interfaces are stable.

## Deliberate non-goals for now

- Full feature-for-feature replacement of the Lazarus GUI.
- Windows support before the serial framing and parity behavior is validated
  on that platform.
- Enabling unverified EBC models merely because they share similar packet
  shapes.

Items may move as real tester captures and field use reveal which seams deserve
more depth.
