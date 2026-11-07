---
id: S13-T4
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/brief_readthrough.py
  - tests/harness/test_brief_readthrough.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Prompt and parser for a model's read-through of a brief -->
# Goal

The harness can ask a model what it would change for a brief and compare the answer with the brief, so unclear briefs are fixed before a run.

# Background

Sprint S13 builds a brief writer so later sprints get fewer bad briefs: free checks in `harness validate` (T1, T2), a cheap read-through by a model (T4, T5), and a skill that writes briefs (T3). Design doc approved 2026-11-07.

S13-T5 runs the prompt through Codex; this task is only the pure prompt, parser and comparison.

# Decisions (do not reopen)

1. New module `harness/brief_readthrough.py`:
    - `def readthrough_prompt(task: TaskSpec, brief_text: str) -> str`: tells the model it is read-only and must not modify files; includes the brief text; asks it to read the repository files it needs and answer ONLY with lines `FILE: <repo path>` (each file it would create or change), `PLAN: <path>: <one line>`, and `UNCLEAR: <question>` (anything the brief leaves open), then a last line `END`.
    - `@dataclass(frozen=True) class Readthrough: files: list[str]; plans: dict[str, str]; unclear: list[str]` and `def parse_readthrough(output: str) -> Readthrough`: tolerant of blank lines, leading `- `, backticks around paths and backslashes (normalize to `/`); ignores other lines.
    - `def compare_readthrough(rt: Readthrough, allowed_paths: list[str]) -> list[str]`: one message per file outside `allowed_paths` (match with `harness.verifier._matches`), one per non-glob allowed path the model would not touch, one per UNCLEAR line. Empty list = the brief reads as intended.
2. No I/O, no subprocess.

# Acceptance criteria

1. Tests: the prompt contains the brief text, "read-only", the three line formats and `END`.
2. Tests: parsing a sample with noise lines, a FILE line written with a leading "- ", backticks and a Windows backslash path, and a PLAN line gives the expected `Readthrough`.
3. Tests: compare reports an outside file, an untouched allowed file, and each UNCLEAR line; a matching answer gives an empty list.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
