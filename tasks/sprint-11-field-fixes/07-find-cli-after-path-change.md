---
id: S11-T7
stage: 2
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S11-T3
  - S11-T4
  - S11-T6
allowed_paths:
  - harness/command_locate.py
  - harness/agy_runner.py
  - harness/agy_usage.py
  - harness/doctor.py
  - tests/harness/test_command_locate.py
  - tests/harness/test_agy_runner.py
  - tests/harness/test_agy_usage.py
  - tests/harness/test_doctor_gitignore.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Find a CLI installed after the session started (report item 9) -->
# Goal

A provider CLI installed (or added to the user PATH) after the shell started is still found, and when it really is missing, the message says what to do.

# Background

agy's installer adds `%LOCALAPPDATA%\agy\bin` to the user PATH. Processes started before that (a Claude Code session, its shells, the harness it starts) keep the old PATH, so every agy call failed with `[WinError 2]` (seen on BookNook and on this repo's S10-T9). The harness fell back to another provider, which is right, but agy never ran and the message did not say why.

# Decisions (do not reopen)

1. New module `harness/command_locate.py` with `locate(command: str) -> str | None`: `shutil.which(command)` first. On Windows only, if not found, read the current user and machine PATH from the registry (`winreg`: `HKEY_CURRENT_USER\Environment` value `Path`, and `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` value `Path`, expanding `%VARS%`) and search those folders with `shutil.which(command, path=...)`. No provider names in this module. Cache per process.
2. `harness/agy_runner.py` and `harness/agy_usage.py` start agy through `locate(config.agy.command)` (an absolute `agy.command` is used as is). When it returns None, the launch failure message is `agy not found on PATH (installed after this shell started? restart the shell or set agy.command to the full path)`.
3. `harness doctor`'s agy check uses the same function and shows the same text. Codex, Cursor and Claude move to `locate` later (the modular provider work); do not change them now.

# Acceptance criteria

1. `tests/harness/test_command_locate.py`: found on PATH; not on PATH but in a faked registry PATH (monkeypatch the registry reader) gives the path; not anywhere gives None; on non-Windows the registry is never read.
2. agy runner and usage tests: a missing command gives the new message as a launch failure.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
