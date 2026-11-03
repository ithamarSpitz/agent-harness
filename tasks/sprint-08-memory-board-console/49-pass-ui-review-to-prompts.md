---
id: S8-T49
stage: 5
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on:
  - S8-T27
  - S8-T44
allowed_paths:
  - harness/runner.py
  - harness/codex_runner.py
  - tests/harness/test_ui_review_prompt_wiring.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Real implementation and retry prompts for Cursor and Codex contain S8-T27's Playwright instruction when `ui_review` is enabled.

# Background

S8-T27 (Nov 2026) added a `ui_review` parameter to `implement_prompt` in `harness/prompts.py`: with UI review enabled, implementation and retry prompts tell the agent to check the UI with Playwright, using the project's `ui_review.command` and `ui_review.url`. Its reviewer found that the callers never pass it: `harness/runner.py` (Cursor) and `harness/codex_runner.py` (Codex) call `implement_prompt` without `self.config.ui_review`, so production prompts never contain the instruction. Those files were outside S8-T27's allowed paths.

# Decisions (do not reopen)

1. Both runners pass `self.config.ui_review` to `implement_prompt` for implementation and retry calls.
2. Review prompts are unchanged.

# Acceptance criteria

1. With fakes and `ui_review.enabled: true`, the prompt text that the Cursor runner and the Codex runner actually send for an implementation call contains the configured command and URL.
2. With `ui_review.enabled: false`, it contains no browser instruction.
3. Existing tests pass.
