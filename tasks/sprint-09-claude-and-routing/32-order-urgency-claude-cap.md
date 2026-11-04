---
id: S9-T32
stage: 8
model_class: hard_worker
timeout_minutes: 60
review: true
allow_protected: false
depends_on:
  - S9-T30
  - S9-T31
allowed_paths:
  - harness/routing.py
  - harness/config.py
  - harness/provider_runner.py
  - harness.yaml
  - tests/harness/test_routing.py
  - tests/harness/test_routing_scenarios.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Provider choice = user's order + "use it before it goes to waste" + a Claude cap that depends on when others come back -->

# Goal

`choose_provider` follows the user's routing rules (Nov 4 2026): a fixed order, quota that would go to waste jumps ahead, Cursor Auto first only while both Cursor pools are at 100%, and Claude capped by a limit that depends on when another provider has usage again. The five scenarios below are the source of truth.

# Background

Today `choose_provider` (`harness/routing.py`) picks the provider with the highest urgency score and uses `providers.priority` only for ties or missing data, and Claude has a fixed reserve (`five_hour_max_used_percent: 50` in `harness/config.py` and `harness/routing.py`). S9-T30 added `cursor.auto_when_exhausted`. S9-T31 added `harness/pace.py` (`full_pace`, `waste`). Antigravity (`agy`) arrives in S10; the user's order already names it first.

# Decisions (do not reopen)

1. **Order.** `providers.priority` is the base order. The user's order is agy, codex, cursor, claude. In this repo's `harness.yaml` set `priority: [codex, cursor, claude]` now (S10 adds `agy` first).
2. **Has usage.** A provider has usage for a call when it is enabled, not disabled for the task, and every window of it is known and below 100% (for agy, S10: the pool of the model the call would use). Unknown usage: it keeps its place in the order and is tried, but is never urgent. Claude also needs its 5-hour window below the Claude cap (decision 5).
3. **Waste and urgency.** For each window: `waste = max(0, left% - hours_to_reset * full_pace)` (S9-T31). A provider's waste is the highest of its windows. A provider with usage and waste above 0 is urgent. Urgent providers go first, highest waste first; then the rest in base order.
4. **Cursor Auto.** For implementation, when `cursor.auto_when_exhausted` is on and both Cursor pools are at 100% (S9-T30), Cursor Auto goes first, before urgency. Auto has no waste and never reviews. Below 100%, Cursor uses its named models in its normal place in the order.
5. **Claude cap.** H = hours until the first other enabled provider (not Claude, not Auto) has usage again: 0 if one has usage now or its usage is unknown; otherwise the earliest time its blocking windows reset. Cap = 60% if H is 1 or less; 90% if H is 5 or more (Claude's own 5-hour window has new quota by then); in between `60 + (H - 1) / 4 * 30`. Claude's weekly window has its own cap: `100 - 7 * days_until_weekly_reset` percent (days as a fraction, for example 5.65 days left gives 60.45%; 0 days gives 100%; never below 0); Claude is usable only while its weekly used percent is below that cap. Replace the fixed `five_hour_max_used_percent` reserve with config `claude.reserve: {min_percent: 60, max_percent: 90, min_hours: 1, max_hours: 5, weekly_percent_per_day: 7}` (these defaults); the old keys still load but are ignored with a logged warning.
6. **Reviews.** Same rules, never the implementing provider and never Auto, unless no other provider has usage: then the implementer may review its own work. If nobody has usage, the review waits.
7. **Each attempt chooses again.** A failed attempt or quota hit re-runs the choice; a provider quota-disabled for the task is skipped until its usage returns.
8. **Nothing available:** wait for the earliest reset, as today.
9. Log the chosen provider and the reason (for example `urgent: waste 50% of codex 5-hour`, `order`, `cursor auto: both pools at 100%`, `claude under cap 75%`) in today's routing log line.
10. `routing.py` must not special-case provider names other than `cursor` (Auto) and `claude` (cap), so a provider named `agy` works in tests now.

# Scenarios (source of truth; each one is a test in `tests/harness/test_routing_scenarios.py`)

Base order agy, codex, cursor, claude. Full pace (injected): Codex 5-hour 30%/h, Claude 5-hour 30%/h, weekly windows 3%/h, monthly 1%/h.

1. **Everything has usage, nothing about to waste.** agy Gemini pool 40% used, resets in 5 days; agy Claude/GPT pool 20%, 5 days; Codex 5-hour 70% used, resets in 3h (30 left, can burn 90, waste 0), weekly 40%, 4 days; Cursor 20% / 30%, 20 days; Claude 5-hour 10%, weekly 30%. Implement: **agy**. Review: **codex**.
2. **Codex about to waste.** Codex 5-hour 20% used, resets in 1h (80 left, can burn 30, waste 50); others as in 1. Implement: **codex**. Review: **agy**.
3. **agy and Codex both urgent.** agy Gemini pool 30% used, resets in 10h (70 left, pace 3, waste 40); Codex 5-hour 50% used, resets in 1h (50 left, can burn 30, waste 20). Implement: **agy**. Review: **codex**.
4. **Cursor pools both at 100%.** Cursor 100% / 100%, resets in 22 days, `auto_when_exhausted: true`; agy Gemini 50%, 5 days; Codex 5-hour 40%, 3h. Implement attempt 1: **cursor with auto**. Review: **agy**. If Auto fails, implement attempt 2: **agy**.
5. **Only Claude has usage.** agy both pools 100%, reset in 2 days; Codex 5-hour 100%, resets in 3h; Cursor 100% / 100% and Auto quota-disabled for the task. Claude 5-hour 50%, weekly 40%. H = 3, cap = 60 + 2/4 * 30 = 75%. Implement: **claude**. With Claude 5-hour at 76%: **wait** (Codex returns in 3h). Review when only Claude has usage: **claude**. With H = 6 (nothing back for 6 hours): cap 90%.

# Acceptance criteria

1. `tests/harness/test_routing_scenarios.py` has one test function per scenario, named `test_scenario_1_everything_has_usage`, `test_scenario_2_codex_about_to_waste`, `test_scenario_3_agy_and_codex_urgent`, `test_scenario_4_cursor_pools_at_100`, `test_scenario_5_only_claude_has_usage`. Each one builds exactly the quotas, reset times and paces written in its scenario (no other values) and asserts every result written there: the implement provider, the review provider, and in 4 and 5 the extra cases (Auto failed; Claude at 76%; H = 6). A reviewer fails the task if any scenario value or expected result differs from this brief.
2. Cap: H = 0.5 gives 60; H = 1 gives 60; H = 3 gives 75; H = 5 gives 90; H = 8 gives 90. Weekly cap: 5.65 days left gives 60.45; 3 days gives 79; 0 days gives 100; with 3 days left, Claude weekly at 80% used means Claude has no usage.
3. No headroom data at all: the order is `providers.priority` exactly.
4. `harness.yaml` priority is `[codex, cursor, claude]`, and `harness validate` passes.
5. Existing tests that check the old urgency-first choice or the fixed 50% reserve are updated to these rules (`tests/harness/test_routing.py` is in allowed_paths).

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
