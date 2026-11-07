---
id: S13-T5
stage: 2
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S13-T4
allowed_paths:
  - harness/codex_runner.py
  - harness/cli.py
  - tests/harness/test_brief_check_cli.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness brief-check <TASK> runs the read-through on Codex -->
# Goal

`harness brief-check <TASK_ID>` asks Codex, read-only, what it would change for that brief and prints where the answer differs from the brief.

# Background

Sprint S13 builds a brief writer so later sprints get fewer bad briefs: free checks in `harness validate` (T1, T2), a cheap read-through by a model (T4, T5), and a skill that writes briefs (T3). Design doc approved 2026-11-07.

`CodexAgentRunner.plan_change` in `harness/codex_runner.py` (around line 450) shows the pattern: `self._invoke(..., phase="plan_repair", read_only=True, ...)` with `self.config.codex.planner_model`. Commands are registered in `harness/cli.py` (`sub.add_parser("validate")` around line 1343, dispatch around line 1508). Prompt, parser and comparison come from `harness/brief_readthrough.py` (S13-T4). Codex only for now: S14 moves providers into adapters.

# Decisions (do not reopen)

1. Add `CodexAgentRunner.brief_check(self, task: TaskSpec, workspace: Path, brief_text: str, timeout_minutes: int, *, model: str, env: dict[str, str] | None = None) -> AgentResult`, calling `_invoke` with `phase="brief_check"`, `read_only=True`, prompt `readthrough_prompt(task, brief_text)`, log name `f"{task.id}-brief-check-codex.log"`.
2. Add `harness brief-check TASK_ID [--timeout-minutes N]` (default 10). It reads the task's brief, runs `brief_check` in the repository root with `config.codex.planner_model`, parses the output, and prints the files, plans, and the comparison messages (or "OK brief reads as intended").
3. Exit codes: 0 no messages, 1 messages, 2 Codex unavailable, out of quota, or the call failed (print the reason; never the raw prompt).
4. It changes no task state and starts no other agent.

# Acceptance criteria

1. `tests/harness/test_brief_check_cli.py` with a fake Codex runner (no real Codex): a matching answer exits 0 and prints OK; an answer naming a file outside `allowed_paths` exits 1 and prints that file; a quota-exhausted result exits 2.
2. A test checks `brief_check` passes `read_only=True` and `phase="brief_check"` to `_invoke`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
