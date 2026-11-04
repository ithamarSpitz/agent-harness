---
id: S9-T20
stage: 6
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T10
  - S9-T19
allowed_paths:
  - harness/provider_runner.py
  - harness/ui_review.py
  - harness/runner.py
  - tests/harness/test_review_edit_guard.py
  - tests/harness/test_ui_review.py
  - tests/harness/test_implementer_cleanup.py
  - tests/harness/test_worktree_guard.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- A reviewer that changes files always fails, in every project -->

# Goal

A review call that creates, edits or deletes files in the task worktree is always failed, whether or not `ui_review` is enabled, so reviewer leftovers are never committed.

# Background

On Nov 3 2026 the Cursor Auto reviewer of S9-T16 wrote three scratch files (`_graph_query_tmp.py`, `_tmp_graph_probe.py`, `_tmp_parse_graph.py`) at the worktree root during its review. Verification had already passed, so the allowed_paths check never saw them, and the harness committed them with the task. `ProviderAgentRunner.review` (`harness/provider_runner.py`) takes `capture_worktree_snapshot` before the review and `worktree_changed_paths` after it only when `config.ui_review.enabled`; agent-harness has `ui_review` disabled, so the guard never ran.

# Decisions (do not reopen)

1. Take the snapshot and run the changed-paths check on every review call (Codex, Cursor and Claude), independent of `ui_review`. Keep the existing failure text (`Reviewer changed files in the worktree` plus the list). Git-ignored files (`graphify-out/`, `.harness/`) are not counted, as today.
2. Port-listener snapshots and process cleanup stay gated by `ui_review` as today.
3. Pass the agent call's PID and start time (from the runners' PID hook) to `cleanup_ui_review_processes` (`agent_pid`, `agent_started_at`, added by S9-T19) for implementation and review calls, so leftover processes of that agent are found even when their command line does not name the worktree.
4. Find out from the installed Cursor CLI (`agent --help`) whether review calls can run in a mode that cannot write files, and whether the harness review command already uses it. Apply it if possible, otherwise list it in OPEN_ISSUES. State the evidence in your report.

# Acceptance criteria

1. With `ui_review` disabled, a fake reviewer that creates an untracked file fails the review with that path listed; one that changes nothing passes.
2. With `ui_review` enabled, behavior is unchanged (existing tests pass), and the cleanup receives the agent PID and start time (fake runner).

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- Existing tests that check behavior you change on purpose are updated to the new behavior (only the ones listed in `allowed_paths`); never weaken a test to make it pass. If another existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
