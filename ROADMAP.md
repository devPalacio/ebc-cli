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
- A supervised PbAc bulk-to-float profile that transitions on the tester's
  source-defined PbAc auto-off state and bounds both charging stages.
- Automatic flushed CSV session logs, including host monotonic elapsed time.
- Best-effort stop on normal completion, operator interrupt, signal, and
  serial failure paths.
- Final active-session summaries with stop reason, elapsed duration, sample
  count, and the last decoded tester status.
- Independent host-side Ah and Wh integration with conservative gap handling.
- Versioned CSV schema metadata and a replayable, sanitized A10H fixture.
- Read-only CSV replay and reporting for offline capacity summaries.
- A model registry that keeps A10H serial settings, scaling, and profile
  packets together, and decodes the source-documented A05 scaling while
  leaving its unverified control path read-only.
- Typed, validated charge and discharge profile objects shared by the CLI
  handlers.
- Semantic completion reasons for the known A10H capacity cutoff status.
- Semantic PbAc auto-off detection for staged normal charging.
- Advisory CLI serial-device ownership with an emergency-stop bypass.
- Read-only inventory of all operating modes defined by the source
  `ebcc.init`, including their packet opcodes and chemistry hints.
- Read-only `plan-mode` packet construction for the source-defined wire modes.
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
  checksum variants encountered in the field. A deterministic synthetic
  fixture and replay validation are now included; real hardware captures are
  still outstanding.
- [x] Compute host-side Ah and Wh independently from sampled current, voltage,
  and monotonic elapsed time.

## Measurement and analysis

- [x] Add a read-only `report` command for summarizing a CSV capacity run.
- [x] Add a replay adapter so logs can be inspected without the tester
  connected.
- [x] Define a documented CSV schema/version.
- [ ] Add a compatibility exporter for the original GUI's simpler CSV format.
- [ ] Add optional JSON output for scripts and monitoring systems.

## Protocol and device coverage

- [x] Move model-specific serial settings, scaling factors, and connection
  packets into a model registry.
- [x] Import the EBC-A05 identity, scaling, and connection frames from the
  upstream `ebcc.init` as a read-only registry entry.
- [ ] Capture and validate EBC-A05 profile behavior before enabling control
  commands for it.
- [ ] Add protocol support for constant-power discharge and the GUI's
  adjustable/continuous commands after capturing representative frames.
- [x] Import the source operating-mode catalog as read-only metadata and
  expose it through `ebc modes` without creating a runtime Lazarus dependency.
- [x] Add checksum-finalized, read-only packet builders and `ebc plan-mode`
  for source-defined modes; live control remains capture-gated.
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
