---
id: S9-T5
stage: 1
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/routing.py
  - tests/harness/test_routing.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Choose the provider for each call -->
# Goal

A pure function decides which provider runs the next call, preferring capacity that would otherwise expire unused, keeping a reserve of Claude for the user's own session, and putting the reviewer on a different provider than the implementer.

# Decisions (do not reopen)

1. `choose_provider(snapshot, *, phase, model_class, enabled, priority, implementer_provider, disabled_for_task, claude_reserve, now) -> Choice(provider, reason, scores)`. No I/O.
2. For each enabled provider not disabled for this task:
   - `free` = 100 − used percent of its tightest window (the window with the highest used percent); for Claude, also subtract nothing but exclude Claude completely when the 5-hour used percent ≥ `five_hour_max_used_percent` or the weekly used percent ≥ `weekly_max_used_percent`;
   - a provider with `free` ≤ 0 is excluded;
   - `urgency` = `free` ÷ max(hours until that window resets, 0.5).
3. Pick the highest urgency. Ties and unknown headroom fall back to `priority` order.
4. Review phase: exclude `implementer_provider` when another provider is available.
5. `reason` is one short line, for example `claude: 78% of the week free, resets in 9h (urgency 8.7); codex out until 17:14`.
6. If no provider is available, return `Choice(None, reason)` so the caller waits as today.
7. Cursor Auto (`auto`, free credit, no quota): implementation attempt 1 always goes to Cursor Auto when Cursor is enabled, before the urgency ranking; attempts 2 and later use the ranking above. Auto is never chosen for the review phase, and never as reviewer fallback; if no other reviewer is available, the review waits.

# Acceptance criteria

- Attempt 1 implement → cursor/auto; attempt 2 → ranking; review with only Auto left → `Choice(None, ...)`.

Table-driven tests with fake snapshots:
1. Claude weekly 22% used resetting in 9 h vs Codex weekly 47% resetting in 6 days → Claude.
2. Claude 5-hour at 55% (over the 50% reserve) → not Claude.
3. Review after a Codex implementation → not Codex when another is available; Codex when it is the only one.
4. All out → `None` with a reason naming each provider.
5. Unknown headroom → priority order.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
