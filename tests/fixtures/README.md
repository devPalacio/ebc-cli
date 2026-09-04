# Test fixtures

`a10h_session.csv` is a small sanitized A10H session fixture. Its status
frames are deterministic, checksum-valid frames assembled from the protocol
fields used by the tests; it is not a claim that these rows came from a
particular physical battery or tester run.

The fixture uses the versioned CLI CSV format:

- the first row is the stable `CSV_HEADER`;
- the second row is the `schema-v1` metadata event;
- rows with a non-empty `raw_frame_hex` are decoded status observations;
- lifecycle and metadata rows have blank measurement fields.

When a real hardware capture is available, it can replace or supplement this
fixture while preserving the raw frame bytes and removing identifying paths,
timestamps, or battery labels as needed.
