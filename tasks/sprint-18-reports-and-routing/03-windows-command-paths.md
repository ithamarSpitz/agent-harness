---
id: S18-T4
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/verifier.py
  - tests/harness/test_verifier.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Verification commands with forward-slash program paths run on Windows -->
# Goal

A verification command such as `node_modules/.bin/deno check scripts/refresh-all.ts` runs on Windows the same as on Linux.

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

`harness/verifier.py` `_run_timed_command` (around line 111) starts each command with `subprocess.Popen(command, shell=True, ...)` (around line 163); on Windows that is `cmd.exe /c`, which reads `node_modules/.bin/deno` as `node_modules` plus a `/` option. A legacy branch around line 128 uses `subprocess.run(..., shell=True)` when tests replace `subprocess.run`.

# Decisions (do not reopen)

1. New pure function `def windows_command(command: str) -> str` in `harness/verifier.py`: split the command on `&&`, `||`, `|` and `;` separators (keep the separators and spacing); in each part, if the first word (after leading spaces) is a relative path containing `/` and not starting with `-` or a quote, replace its `/` with `\`. Only the first word changes; arguments such as `scripts/refresh-all.ts` stay as written. A first word in double quotes is handled the same way inside the quotes.
2. Both start paths use `windows_command(command)` only when `os.name == "nt"`; logs and failure messages keep the original command text.
3. No other platform behavior changes.

# Acceptance criteria

1. Tests in `tests/harness/test_verifier.py` for `windows_command`: `node_modules/.bin/deno check scripts/refresh-all.ts` becomes `node_modules\.bin\deno check scripts/refresh-all.ts`; `npm run test && node_modules/.bin/tsc -p .` changes only `tsc`'s path; `python -m pytest -q tests/harness` is unchanged; `"./tools/x.cmd" a/b` becomes `".\tools\x.cmd" a/b`; `C:/x/y.exe` is left alone (absolute path).
2. A test with `os.name` patched to `nt` and a fake `Popen` checks the converted command is what starts and the failure message names the original command.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
