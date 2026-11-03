---
id: S8-T50
stage: 5
model_class: worker
timeout_minutes: 20
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - tests/harness/test_proc_tree.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

`test_peak_sampler_tracks_maximum_and_stop_is_timely` in `tests/harness/test_proc_tree.py` passes reliably on a busy computer.

# Background

On Nov 3 2026 it failed during S8-T8's verification (422 other tests passed) while two task verifications ran at once. It sleeps `interval * 3` (0.15 s) and expects the sampler to have seen all three fake snapshots, and it requires `stop()` to return within `interval * 1.5` (75 ms). Both fail when the machine is loaded.

# Decisions (do not reopen)

1. Wait for the samples by condition, not by a fixed sleep: poll until the fake lister has been called at least 3 times, with a generous upper limit (for example 10 s) that fails the test only if never reached.
2. Keep the "stop is timely" check but with a margin that still proves the sampler does not wait a full long interval: use a long sampling interval (for example 5 s) and require `stop()` to return within 2 s.
3. Keep the peak assertion (3 GB) unchanged. Only this test file changes.

# Acceptance criteria

1. The test passes 20 times in a row while another `pytest -q tests/harness` runs at the same time (state the evidence in your report).
2. It still fails if `stop()` waited for the full interval (explain how the 5 s / 2 s values prove it).
