---
id: S9-T4
stage: 1
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/headroom.py
  - tests/harness/test_headroom.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Live headroom snapshot for all providers -->
# Goal

One function gives the scheduler a fresh, cheap view of how much each provider has left and when it resets.

# Decisions (do not reopen)

1. New `harness/headroom.py`: `Headroom(provider, used_percent, resets_at, window, known)` per provider and window, and `headroom_snapshot(max_age_seconds=300) -> dict[str, list[Headroom]]`.
2. Data comes from the existing functions in `harness/limits.py` (Codex live app-server, Cursor dashboard usage, Claude live usage); do not duplicate their network code. Cursor: one entry for the pool the harness uses (Composer/Grok) and one for the other-models pool. Codex and Claude: the 5-hour and weekly windows.
3. Cache the snapshot in memory and in `~/.harness/headroom-cache.json` for `max_age_seconds`, so parallel calls and several projects do not query the providers again and again. A failed source gives `known=False` for that provider, never an exception.
4. The limits functions are injectable; tests use fakes.

# Acceptance criteria

1. Fake sources produce the expected entries for all three providers.
2. A second call within the max age uses the cache (no source calls); an old cache is refreshed.
3. A failing source gives `known=False` and the others still work.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
