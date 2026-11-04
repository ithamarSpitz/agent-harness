---
id: S9-T30
stage: 7
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S9-T26
  - S9-T20
allowed_paths:
  - harness/config.py
  - harness/routing.py
  - harness/provider_runner.py
  - harness.yaml
  - tests/harness/test_cursor_free_only.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Cursor runs only when it cannot spend the user's included Cursor usage -->

# Goal

With `cursor.free_only: true`, the harness calls Cursor only while both Cursor usage pools are at 100%, so harness runs never use the user's included Cursor usage.

# Background

The user's rule (Nov 4 2026): use Cursor Auto only when usage is at 100% on both Cursor pools ("Cursor models (Composer, Grok)" and "Other models"), so it won't spend the user's own usage. Below 100%, any Cursor call takes from the included usage the user wants for their own work. `harness/limits.py` `load_cursor` already reads both (`auto_percent_used`, `api_percent_used`); `harness/headroom.py` exposes them as cursor windows `composer_grok` and `other_models`. Today the routing does the opposite: a cursor window at 100% counts as no capacity, and the Auto-first rule (S9-T5 decision 7, wired by S9-T26) returns Cursor without looking at usage at all.

# Decisions (do not reopen)

1. New `CursorConfig.free_only: bool = False` (`harness/config.py`). Set `free_only: true` in this repo's `harness.yaml` under `cursor:`.
2. With `free_only` on, Cursor is eligible (for any phase and model) only when the latest headroom shows both cursor windows known and at 100% or more. Unknown, missing or stale cursor data means not eligible.
3. When eligible under `free_only`, being at 100% is not "no capacity" for Cursor; when not eligible, Cursor is treated as unavailable with reason `cursor: would use included usage (<composer>% / <other>%)` or `cursor: usage unknown`, shown in the existing routing log line.
4. This applies everywhere the harness picks Cursor: `choose_provider` (including the Auto-first branch) and every fallback in `ProviderAgentRunner` (for example after a Codex quota hit). If no other provider is available, the task waits for quota as today; it never falls back to Cursor below 100%.
5. A real Cursor call that still reports a usage limit is handled as today (quota-disable for the task).
6. With `free_only` off (the default), behavior is unchanged, so existing tests keep passing.

# Acceptance criteria

1. Fake headroom, `free_only: true`: cursor at 100%/100% → an attempt-1 implement goes to Cursor Auto; cursor at 100%/40% or 40%/100% → it goes to Codex; cursor unknown → Codex.
2. `free_only: true`, Codex quota-disabled for the task, cursor at 50%/50% → the task waits for quota, no Cursor call is made.
3. `free_only: false`: same inputs give today's results.
4. `harness.yaml` has `free_only: true`, and `harness validate` passes.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
