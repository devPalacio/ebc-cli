---
name: create-charge-profile
description: Create bounded battery charge profiles with tests.
version: 0.1.0
author: Jay, Hermes Agent
license: GPL-3.0-only
platforms: [linux]
metadata:
  hermes:
    tags: [battery, charging, ebc, profiles]
    related_skills: []
---

# Create a charge profile

Turn a battery's documented charging requirements into a bounded `ebc-cli`
recipe. Reuse an existing tester mode and the shared encoder. Creating a
profile does not authorize connecting to a tester or starting a charge.

## When to use

- Choose CLI settings for a specified battery and charging purpose.
- Add a named charging recipe or a documented multi-stage profile.
- Review a proposed profile's limits, completion rules, and tests.

Do not use this workflow to invent a chemistry algorithm, recover an unknown
battery, enable an unsupported tester, or bypass a battery protection system.

## Prerequisites

Work from the repository root. Use Python 3.14 or newer and `uv`.
The current live-control target is the AC-powered EBC-A10H; A05 control is
disabled. Read the implementation rather than assuming this guide defines it:

- [Profiles](../../src/ebc_cli/profiles.py) for battery-specific limits and stages.
- [Modes](../../src/ebc_cli/modes.py) for `EbcModeCommand` and packet builders.
- [Device](../../src/ebc_cli/device.py) for model identification and `begin_mode`.
- [Sessions](../../src/ebc_cli/session.py) for deadlines, completion, and STOP.
- [Usage](../../docs/usage.md), [library use](../../docs/library.md),
  [operating limits](../../docs/ebc-a10h-operating-limits.md),
  and [capture evidence](../../docs/a10h-mode-captures.md) for current limitations.

## Procedure

### 1. Establish the battery requirements

Record the battery model, chemistry, series-cell count, nominal capacity,
charging purpose, and condition reported by the operator. Find the exact
manufacturer's charging instructions. Cite the source and the section or page
for current, voltage, termination, and applicable temperature limits.

Separate manufacturer limits from choices made for this recipe. If C-rate is
used, calculate amperes from the stated capacity with a tool and show the
calculation. Do not infer chemistry from terminal voltage or copy a current
from an unrelated battery. Ask for any missing fact that changes the settings.

**Complete when:** the proposed settings have cited limits, units, and a
reason for each chosen value, or the missing information is named explicitly.

### 2. Use the smallest existing path

Prefer an existing named command with explicit options when it already fits.
Use `charge` for bounded CCCV charging with a host end-current threshold and
`capacity-test` for a CC discharge with explicit cutoff, current, and timeout.
Neither command infers chemistry. The capacity profile is not restricted to
12 V lead-acid batteries; do not add cell-size-specific packet encoders.
Keep lead-acid float holds separate from lithium charge termination.

Charge end-current, voltage qualification, continuous dwell, and maximum
observation gap are host policy. They are not extra CCCV packet fields.
Device auto-off is a distinct outcome, not proof that the host threshold was
met. Cite manufacturer termination requirements and choose the observation
policy explicitly when the defaults do not fit. A timeout is not completion.
Use streamed post-start observations, not cached CONNECT replies, as charge
completion evidence. Measure dwell and sample gaps at receipt, before output
or logging can delay processing. Recheck the deadline before honoring completion.
Check discharge power as well as current and voltage; independently valid
settings can exceed the load power ceiling when multiplied together.

Use `run-mode` for a supported single operation that does not need a new
battery-specific policy. Add Python code only for a genuinely reusable named
recipe or a required sequence that existing commands do not express.

The repository does not load user-defined JSON or YAML profile files. Do not
invent a preset file, a `--profile` option, or a new loader as part of this task.

For each stage, specify the mode, current limit, cell count or target voltage,
positive finite host timeout, and exact completion condition. Account for
charger, supply, battery, and any protection-system limits. If the required
termination or temperature monitoring is unavailable, identify the gap rather
than claiming the CLI enforces it. Tester maxima are not battery recommendations.

**Complete when:** the recipe fits an existing operation, or the smallest
necessary code change and unsupported requirements are identified.

### 3. Build the recipe, not another encoder

For code changes, inspect the existing profile factories, argument parser,
command handlers, and profile tests before editing.

- Keep chemistry-specific restrictions and defaults in `profiles.py`.
- Return `EbcModeCommand` from `start_command`, using an existing mode constant.
- Use `build_mode_packet` for bytes and `EbcDevice.begin_mode` for execution.
  Do not add per-profile byte arrays, checksums, opcodes, or an alternate start path.
- Reuse `EbcModeCommand.validate_live` and the model's registered capabilities.
  Do not enlarge a hardware envelope to make a recipe pass validation.
- Preserve each profile's narrower battery limits. A float profile must not
  silently gain the generic CCCV voltage range.
- Keep execution inside `SupervisedSession` and the existing session runners.
  Preserve model checks, explicit execution confirmations, logging, and STOP cleanup.
- Library callers enter through `run_profile`/`run_bulk_to_float`, which own the
  supervised lifecycle; command handlers only format their result. Do not add a
  second start path or a parallel runner for one recipe.
- For multiple stages, define the transition evidence and timeout behavior.
  A timeout, missing sample, or stale completion byte must not start another stage.
- Do not merge charge-profile completion policy with generic mode completion
  merely because both now use the same encoder.

Keep GPL notices on new source files. Change usage documentation only for the
behavior actually implemented. Hardware observations must not be invented.

**Complete when:** the recipe uses the existing encoder and supervised lifecycle,
with every new stage and completion condition represented explicitly.

### 4. Verify without hardware

Use the agent's terminal tool for commands and its file tools for edits.
Run `uv run python -m unittest discover -s tests -v`, `uv run ruff check .`,
and `uv run ruff format --check .` from the repository root.

Add tests for valid values, each limit boundary, missing parameters, non-finite
values, and a positive integer timeout. Invalid live requests must fail before
opening the serial port. Keep literal expected packet bytes independent of the
encoder under test. Test that profile restrictions remain narrower than generic
mode limits where intended.

Exercise normal completion, timeout, serial errors, logging errors, and
interrupt cleanup with mocks or pseudo-terminals. For staged recipes, test
transition evidence, stale completion, and expiry without starting the next stage.
Use [profile tests](../../tests/test_profiles.py),
[lifecycle tests](../../tests/test_commands_lifecycle.py), and
[deadline tests](../../tests/test_session_deadlines.py) as examples.

This is a software example using existing test values, not a battery recommendation:

```bash
uv run ebc plan-mode c_CCCV --current 0.50 --voltage 13.7 --timeout-minutes 120
```

The offline example prints `FA71003205AA007894F8`. `plan-mode` checks packet
construction, not the complete live envelope or battery suitability. Also test
`validate_live` with the actual registered model capabilities. Do not run
`status`, `monitor`, or a live start to verify profile creation.

**Complete when:** tests and checks pass, the planned packet is explained,
and the handoff distinguishes software verification from hardware validation.

## Pitfalls

- Recovery, float, generic charging, and staged charging have different completion
  policies. Their command bytes alone do not describe the whole recipe.
- A05 scaling support does not authorize A05 charging. Unknown IDs remain read-only.
- A valid status frame is not proof that a setpoint was accepted or STOP executed.
- Host deadlines and STOP are best-effort. They do not provide thermal protection
  or cleanup after SIGKILL, a hung host, or loss of power or communication.
- Do not assert `--i-am-present` on an operator's behalf. A later live test needs
  explicit approval for the exact recipe and confirmation that the operator is present.

## Handoff

Provide the battery identity and cited requirements, settings and units, stage
transitions, timeout and completion behavior, unsupported protections, changed
files, and executed test results. State whether any hardware test occurred.
Provide a start command without execution confirmations for operator review.
Do not start the recipe as part of the handoff.
