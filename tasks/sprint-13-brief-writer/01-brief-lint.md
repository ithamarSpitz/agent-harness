---
id: S13-T1
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/brief_lint.py
  - tests/harness/test_brief_lint.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Pure brief checks that cost no tokens -->
# Goal

A pure function finds the brief mistakes we keep making, before any agent runs.

# Background

Sprint S13 builds a brief writer so later sprints get fewer bad briefs: free checks in `harness validate` (T1, T2), a cheap read-through by a model (T4, T5), and a skill that writes briefs (T3). Design doc approved 2026-11-07.

Mistakes seen in S11 and on BookNook: a file the task must change missing from `allowed_paths` (S11-T7 left out `harness/cli.py`); a protected file listed without `allow_protected: true`; affected tests not listed; the "stop if a test outside allowed_paths fails" rule missing; tasks too big to pass review. Briefs look like `tasks/sprint-12-agy-test-isolation/01-no-real-agy-in-tests.md`: YAML frontmatter, then `# Goal`, `# Background`, `# Decisions (do not reopen)`, `# Acceptance criteria` (numbered list), `# Rules`.

# Decisions (do not reopen)

1. New module `harness/brief_lint.py` with `@dataclass(frozen=True) class BriefFinding: level: str  # "error" or "warning"; code: str; message: str` and `def lint_brief(task: TaskSpec, text: str, root: Path, protected: list[str]) -> list[BriefFinding]`. `text` is the whole brief file; `TaskSpec` is `harness.models.TaskSpec`. No I/O except `Path.exists()` under `root`.
2. Path matching reuses `harness.verifier._matches(path, pattern)`; do not write a new matcher.
3. Sections are markdown headings of level 1 or 2; a section runs to the next heading of the same or higher level. Match heading names case-insensitively by prefix ("Decisions" matches "Decisions (do not reopen)").
4. Checks, with these exact codes:
    - `protected` (error): `task.allow_protected` is false and an `allowed_paths` entry matches a `protected` pattern. For a glob entry, test its fixed prefix (the text before the first `*`, `?` or `[`).
    - `missing-goal`, `missing-criteria` (warning): no Goal heading; no Acceptance criteria heading or it has no list item.
    - `stop-rule` (warning): the text does not contain "outside allowed_paths" or "outside `allowed_paths`" (case-insensitive).
    - `named-not-allowed` (warning): a backticked repo path (contains `/` and ends in `.py`, `.md`, `.yaml`, `.yml`, `.toml`, `.json`, `.js` or `.ts`) in the Decisions or Acceptance criteria section that no `allowed_paths` entry matches. One finding per path.
    - `allowed-missing` (warning): a non-glob `allowed_paths` entry that does not exist under `root` and whose path does not appear anywhere in the body text (new files must be named in the brief).
    - `no-tests` (warning): `allowed_paths` has a `.py` entry outside `tests/` but no entry under `tests/`.
    - `too-big` (warning): more than 6 `allowed_paths` entries or more than 6 acceptance criteria items; the message says to consider splitting.
5. Findings are sorted errors first, then by code, then message. Messages name the path or count involved.

# Acceptance criteria

1. `tests/harness/test_brief_lint.py` has one test per code that triggers it, plus one clean brief (built in the test with `tmp_path`) that returns no findings.
2. A glob entry `tasks/**` with protected `tasks/**` gives `protected`; the same with `allow_protected=True` gives none.
3. A path named only in Background does not give `named-not-allowed`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
