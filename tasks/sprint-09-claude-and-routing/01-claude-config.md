---
id: S9-T1
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/config.py
  - tests/harness/test_config_claude.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Configuration for the Claude Code provider -->
# Goal

`harness.yaml` can describe the Claude Code provider: whether it is on, its program, its model ladder and its reserve.

# Decisions (do not reopen)

1. New `ClaudeConfig` section `claude:` in `harness/config.py`, loaded like `codex:` and `cursor:`:
   - `enabled: false` (default off; turned on per project),
   - `command: claude`,
   - `implementation_sequence` (list of `{model, effort}`, one per attempt), default:
     1. `{model: sonnet, effort: medium}`
     2. `{model: sonnet, effort: high}`
     3. `{model: opus, effort: high}`
   - `review_model: sonnet`, `review_effort: high`,
   - `escalation_model: opus`, `escalation_effort: high`,
   - `permission_mode: bypassPermissions` (the harness only runs agents inside its own worktrees),
   - `reserve: {five_hour_max_used_percent: 50, weekly_max_used_percent: 70}`: harness agents may start a Claude call only while Claude usage is below both.
2. `providers.priority` accepts `claude` as a provider name.
3. Validation: effort must be one of `low`, `medium`, `high`, `max`; percents between 0 and 100; an empty sequence is an error. Existing configs load unchanged.

# Acceptance criteria

1. Loading without `claude:` gives the defaults above and `enabled: false`.
2. A full `claude:` section loads every key; invalid effort or percent fails with a message naming the key.
3. `providers.priority: [codex, claude, cursor]` loads.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
