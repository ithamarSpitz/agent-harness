---
id: S8-T52
stage: 9
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/agent_ledger.py
  - tests/harness/test_agent_ledger.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

The shared agent ledger (`harness/agent_ledger.py`) never loses or skips records because of a short Windows file-sharing error.

# Background

On Nov 3 2026 `test_two_processes_update_the_ledger_in_parallel` failed during S8-T48's verification (486 other tests passed): a worker's `register_agent` returned `""`. On Windows, `os.replace(temp, ledger.json)` and reading `ledger.json` can fail for a moment with `PermissionError` (another process or antivirus has the file open just after the other harness replaced it). Today:

- `_write` failing makes `_change` log a warning and return an empty ledger, so the change is silently lost (`register_agent` returns `""`).
- `_read` treats any `OSError` as a corrupt file and returns an empty ledger, which `_change` then **writes back**, deleting every other harness's records.

# Decisions (do not reopen)

1. `os.replace` in `_write` and the file read in `_read` retry on `PermissionError` (and Windows sharing violations surfaced as `OSError` with `winerror` 5, 32 or 33) for up to 2 seconds with short sleeps, still inside the lock.
2. In `_read`, an `OSError` that persists after the retries is raised, not treated as corruption, so `_change` does not overwrite the file (it logs its existing warning and returns the empty ledger without writing). Only real content errors (invalid JSON, wrong shape, `UnicodeError`) replace the file with an empty ledger as today.
3. No change to the lock, the record format or the public functions.

# Acceptance criteria

1. With a fake `os.replace` that raises `PermissionError` twice and then works, `register_agent` returns the id and the record is persisted.
2. With a fake read that keeps raising `PermissionError`, the existing ledger file is left byte-for-byte unchanged.
3. Invalid JSON is still replaced with an empty ledger.
4. The two-process test passes 20 times in a row (state the evidence in your report).
