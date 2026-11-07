# Test fixtures

`a10h_session.csv` is a small sanitized A10H session fixture. Its status
frames are deterministic, checksum-valid frames assembled from the protocol
fields used by the tests; it is not a claim that these rows came from a
particular physical battery or tester run.

`a10h_live_cycles.csv` is a sanitized fixture from supervised A10H charge and
discharge checks. Its timestamps and event labels are normalized, while the
raw status frames and decoded measurements are retained. It intentionally
contains the alternating state/timer pairs observed during live operation.

The fixture uses the versioned CLI CSV format:

- the first row is the stable `CSV_HEADER`;
- the second row is the `schema-v1` metadata event;
- rows with a non-empty `raw_frame_hex` are decoded status observations;
- lifecycle and metadata rows have blank measurement fields.

When adding another real hardware capture, preserve the raw frame bytes and
remove identifying paths, timestamps, or battery labels as needed.

The short mode smoke-check notes are in
`docs/a10h-mode-captures.md`; they remain evidence only until sustained load
behavior is observed.
