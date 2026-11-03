---
id: S9-T10
stage: 3
model_class: hard_worker
timeout_minutes: 55
review: true
allow_protected: false
depends_on:
  - S9-T2
  - S9-T4
  - S9-T5
allowed_paths:
  - harness/routing.py
  - harness/provider_runner.py
  - harness/cli.py
  - tests/harness/test_provider_choice.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Use Claude and the provider choice in real calls -->
# Goal

Real implementation, retry and review calls go to the provider chosen by S9-T5, including Claude Code.

# Decisions (do not reopen)

1. `harness/provider_runner.py` builds a Claude runner (S9-T2) when `claude.enabled`, next to the Codex and Cursor runners.
2. Before each call, get `headroom_snapshot()` (S9-T4) and call `choose_provider` (S9-T5) with the phase, model class, enabled providers, `providers.priority`, the provider that implemented the task (for reviews), the providers disabled for this task (quota, auth, launch failures, as today), and the Claude reserve from config.
3. The chosen provider runs the call. If it then reports quota/auth/unavailable, disable it for the task as today and choose again; if none is left, wait as today.
4. Log the choice and its `reason` in the task log and in the usage record (`routing_reason`).
5. With `claude.enabled: false` and no headroom data, the order is exactly today's (`providers.priority`), so existing projects behave the same.
6. `harness doctor` reports whether the `claude` program is available and logged in when Claude is enabled (by running `claude --version`; never read credentials).

# Notes from stage 1 (Nov 2026)

- `choose_provider` (S9-T5, `harness/routing.py`) takes a nested per-provider dict snapshot, while `headroom_snapshot` (S9-T4, `harness/headroom.py`) returns its own shape. Convert in one small adapter (in `harness/provider_runner.py` or `harness/routing.py`), with a test that feeds a real `headroom_snapshot` result (fake providers) into `choose_provider`.
- `_until_label` in `harness/routing.py` formats reset times in UTC. Show local time instead (`astimezone()` with no argument), since these reasons are shown to the user.

# Acceptance criteria

1. Fake headroom favoring Claude → the implementation call goes to the fake Claude runner, with the reason logged.
2. Review after a Claude implementation → another provider when available.
3. Claude over the reserve → not used.
4. Claude disabled → calls and order exactly as before (existing tests unchanged).

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
