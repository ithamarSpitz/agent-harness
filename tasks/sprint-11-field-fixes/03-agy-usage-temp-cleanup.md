---
id: S11-T3
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/agy_usage.py
  - tests/harness/test_agy_usage.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- agy usage: a temp-folder cleanup error is not 'failed to start' (report item 8) -->
# Goal

`harness limits` and the headroom snapshot show agy usage on Windows even when agy leaves a file open in its temporary working folder.

# Background

BookNook, 0.20.0: `Antigravity: usage unavailable (agy usage command failed to start: [WinError 32] The process cannot access the file because it is being used by another process: '...\Temp\harness-agy-usage-XXXX')`. `fetch_agy_usage` (`harness/agy_usage.py`, around line 100) runs agy inside `tempfile.TemporaryDirectory(...)`. The `except OSError` around the whole block also catches the error raised when the folder is removed after agy exits (agy, or a helper it starts, still holds a file there), and reports it as "failed to start" although the output was already captured.

# Decisions (do not reopen)

1. Use `tempfile.TemporaryDirectory(prefix=..., ignore_cleanup_errors=True)` so a cleanup failure never hides a good result. Keep the output parsing as is.
2. Only an `OSError` from starting the process is reported as "failed to start"; keep start and cleanup clearly separate in the code.
3. A leftover folder is harmless (it is in the system temp folder); do not add retries or extra cleanup.

# Acceptance criteria

1. A test with a fake `run` that succeeds and a forced cleanup error (patch the cleanup to raise `PermissionError`) still returns the parsed buckets.
2. A fake `run` raising `FileNotFoundError` is still reported as failed to start.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
