# Agent Operating Contract

This repository is the agent harness itself: a Python orchestrator (`harness/`) that runs Codex and Cursor coding agents against any Git project. It is developed by its own previous version, installed separately; your changes here never affect the running orchestrator.

## Source-of-truth order

1. The current task brief under `tasks/`.
2. `README.md` (what the harness does) and `HARNESS-OPERATOR.md` (how it is operated).
3. Existing implementation and tests.

Never silently resolve a conflict between these. Stop and report it.

## Context policy

1. Read the current task brief first.
2. Read only the files the brief names and the code you actually change.
3. `harness/cli.py`, `harness/scheduler.py` and `harness/runner.py` are large. Search them for what you need; do not read them whole.

## Global rules

- Python 3.11+, standard library plus PyYAML. No new runtime dependencies unless the brief allows it.
- The harness must keep working on Windows and POSIX. Anything that touches processes, paths or shells needs both branches.
- The harness is project-agnostic. Nothing project-specific (BookNook, npm, Supabase) goes into `harness/`.
- New config keys get a default that keeps today's behavior, so existing `harness.yaml` files load unchanged.
- Tests never call real Codex, Cursor, Graphify, Playwright or the network. Use fake commands (small Python scripts) and temp Git repos, as the existing tests do.
- Work only on the current task. Do not modify files outside `allowed_paths`. Do not edit `harness.yaml`, `tasks/**`, `AGENTS.md` or `HARNESS-OPERATOR.md`.
- Do not weaken or delete tests to make verification pass.
- Do not commit; the harness owns commits and merges.

## Commands

```text
python -m pytest -q tests/harness     all tests
python -m ruff check harness tests    lint
python -m mypy harness                types
```

## Definition of done

The harness accepts a task only when task and global verification pass, changed files stay within scope, review passes, and the stage merge passes.

When an implementation attempt ends, report:

- `STATUS`: implementation finished / blocked
- `CHANGED_FILES`
- `TEST_RESULTS`
- `OPEN_ISSUES`
