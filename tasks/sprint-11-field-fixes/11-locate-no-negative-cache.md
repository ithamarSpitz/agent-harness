---
id: S11-T11
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/command_locate.py
  - tests/harness/test_command_locate.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- locate() must not cache a failed lookup -->
# Goal

A command installed after the harness process started is found on the next `locate()` call, even if an earlier call in the same process did not find it.

# Background

Found by the S11-T9 docs review on 2026-11-06 (two failed reviews): the docs describe this behavior, the merged code does not do it. A docs-only task cannot fix code, so this task fixes the code and S11-T9 documents it afterwards.

`harness/command_locate.py` decorates `locate` with `@functools.cache`, so a `None` result is cached for the life of the process. If usage polling, doctor or an agy call looks up `agy` before it is installed, a later registry PATH update stays invisible. The reviewer reproduced it: `None` before and after the registry lookup started succeeding.

# Decisions (do not reopen)

1. Cache only successful lookups (a found path). A miss is never cached, so the next call checks PATH and the registry again.
2. Keep the public signature `locate(command: str) -> str | None` and the existing callers unchanged.
3. If tests need it, expose a way to clear the success cache (for example `locate.cache_clear()` or a module-level helper); keep it small.

# Acceptance criteria

1. New test: first call with the registry lookup returning no match gives `None`; then the fake registry PATH gains the folder; the second call returns the path.
2. New test: a found path is returned from cache on the second call without reading the registry again.
3. Existing `tests/harness/test_command_locate.py` tests still pass.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
