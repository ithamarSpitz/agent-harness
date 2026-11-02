---
id: S8-T7
stage: 2
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S8-T3
allowed_paths:
  - harness/console_commands.py
  - tests/harness/test_console_commands.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Console: command registry and read-only commands -->
# Goal

The commands the user types in the console, starting with the ones that only read.

# Decisions (do not reopen)

1. `harness/console_commands.py`:
   - A registry: `Command(name, args_help, help, changes_state, handler)`; `run_command(line, context) -> list[str]` parses the line, finds the command, returns output lines; unknown commands return one line pointing to `help`.
   - `ConsoleContext` gives handlers what they need (runtime, sprint id, a way to call existing CLI functions, a confirm callback).
   - Read-only commands, each calling the same code as its CLI command where one exists:
     - `help`: all commands with one line each;
     - `status`: empty output (the board shows status);
     - `task <id>`: status, provider/model, attempt, elapsed, allowed paths, dependencies, added flag;
     - `why <id>`: what `harness failure` shows for that task (last reviewer findings or error);
     - `log <id>`: the last agent message of the latest call only, not the full log;
     - `diff <id>`: `git diff --stat` of its worktree against its base;
     - `usage`: the output of `harness limits`.
2. Read-only commands never write `state.json` (test it).

# Acceptance criteria

1. Each command returns the expected lines for a fake runtime; unknown ids give one clear line.
2. `help` lists every registered command.
3. A test proves read-only commands leave `state.json` byte-identical.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
