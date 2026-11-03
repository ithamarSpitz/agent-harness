---
id: S9-T28
stage: 5
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S9-T9
allowed_paths:
  - harness/winplatform.py
  - harness/manifest.py
  - tests/conftest.py
  - tests/harness/test_winplatform.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Test runs and manifests never hang on a stuck Windows WMI query -->

# Goal

A slow or stuck Windows WMI service can no longer freeze a test run or the harness through Python's `platform` module.

# Background

On Nov 3 2026 two S9 verification runs (S9-T12 and S9-T9) each hung for the full 30-minute limit and burned an attempt. Both thread dumps show a pytest-xdist worker stuck at start in `platform._wmi_query` (Python 3.12 `platform.py`, called from xdist `getinfodict` -> `platform.platform()`). `_wmi_query` has no timeout. Every caller in `platform.py` (`_win32_ver`, `_get_machine_win32`, `_Processor.get_win32`) catches `OSError` and falls back to `sys.getwindowsversion()` or environment variables, which never block. The harness itself calls `platform.platform()` in `harness/manifest.py`.

# Decisions (do not reopen)

1. New module `harness/winplatform.py` with `disable_wmi_queries() -> None`: on Windows only, replaces `platform._wmi_query` with a function that raises `OSError("WMI disabled by harness")`, so `platform` uses its non-blocking fallbacks. Does nothing on other systems or if `platform` has no `_wmi_query`. Safe to call more than once.
2. `tests/conftest.py` calls it at import time (top of the file), so every xdist worker has it before `pytest_sessionstart`.
3. `harness/manifest.py` calls it before `platform.platform()`.
4. Results may be slightly less detailed (for example processor name from `PROCESSOR_IDENTIFIER`); that is accepted.

# Acceptance criteria

1. Test: after `disable_wmi_queries()` with `platform._wmi_query` swapped for a function that would sleep forever (monkeypatched, restored after), `platform.platform()`, `platform.machine()` and `platform.processor()` return strings within 5 seconds (clear `platform._uname_cache` and any cached processor value first). Skipped on non-Windows.
2. Calling it twice is fine; on non-Windows it does nothing.
3. The full suite passes.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
