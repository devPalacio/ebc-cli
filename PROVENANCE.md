# Source provenance

`ebc-cli` is a Python CLI port based on
[JOGAsoft/EBC-controller](https://github.com/JOGAsoft/EBC-controller),
distributed under GNU GPL version 3 only (`GPL-3.0-only`).
See [LICENSE](LICENSE) for the terms and [NOTICE](NOTICE) for attribution
and the dated port notice.

## Upstream reference

The port uses controller revision
[`ae13528fe1724ef6825cf6608bb2a033cb1a45a6`](https://github.com/JOGAsoft/EBC-controller/tree/ae13528fe1724ef6825cf6608bb2a033cb1a45a6).

- `ebcc.init` supplies the mode names, packet opcodes, completion bytes,
  model IDs, and scaling factors represented in the Python registries.
- `main.pas` supplies parameter encoding, checksum and status-field logic,
  and the software constant-resistance reference behavior.
- `stepform.pas` and `stephelp.txt` describe the GUI's host program language.
  The CLI does not implement that language.
- The complete GPLv3 text in `LICENSE` is copied verbatim from the
  upstream repository's `LICENSE`.

The port replaces the GUI with command-line operations, bounded host sessions,
and CSV logging and replay. It is not an upstream release. Original authors
retain rights in their contributions; the Python CLI contributions are
copyright 2026 Jay.

Manufacturer references are listed in
[operating limits](docs/ebc-a10h-operating-limits.md). The
[fixture README](tests/fixtures/README.md) distinguishes generated samples
from recorded observations.

## Distribution

The wheel and source distribution include `LICENSE` and `NOTICE`.
The source distribution includes the Python source, build configuration,
lockfile, tests, and documentation. Build instructions are in [README.md](README.md).
When publishing packages, provide the matching source distribution alongside
the wheel so recipients can obtain and rebuild the corresponding source.
