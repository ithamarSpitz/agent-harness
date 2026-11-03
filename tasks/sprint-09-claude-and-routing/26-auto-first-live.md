---
id: S9-T26
stage: 4
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S9-T10
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_auto_first.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Cursor Auto really takes first implementation attempts; routing uses live headroom in every project -->

# Goal

The routing rules from S9-T5 run in real projects: the first implementation attempt of a task goes to Cursor Auto, later attempts and reviews are chosen by live headroom and never review on the implementing provider, whether or not Claude is enabled.

# Background

S9-T5 (`harness/routing.py`, `choose_provider`) has the rule "the first implementation attempt is Cursor Auto (`model_class == "auto"`), Auto never reviews". S9-T10 wired `choose_provider` into `ProviderAgentRunner`, but (a) it always passes `task.model_class` (for example `worker`), so the Auto-first rule never fires, and (b) it uses the headroom snapshot only when `claude.enabled`, so projects without Claude keep the old fixed `providers.priority` order even when live headroom is known. The user's rule (Nov 3 2026): Auto is always the first implementation attempt and never the reviewer.

# Decisions (do not reopen)

1. For implementation attempt 1 of a task, when Cursor is enabled and `cursor.models` has an `auto` model, call `choose_provider` with `model_class="auto"` and run Cursor with that model. Later attempts use the task's model class as today. A task brief can opt out with `model_class: hard_worker` or higher (those keep their class on attempt 1).
2. Use the live headroom snapshot for choices in every project. When no headroom data is available, the order is exactly `providers.priority` (as S9-T10 decision 5 intended).
3. Reviews never go to Cursor Auto and never to the provider that implemented the task (already recorded as `provider.implementer`); if no other reviewer is available, the review waits as today.
4. Log the choice and reason as S9-T10 does.
5. If part of this already exists when you start, keep it and add the missing parts and the tests.

# Acceptance criteria

1. Fake runners, a `worker` task, Codex and Cursor both available: attempt 1 runs on Cursor with the Auto model; a failing attempt 1 sends attempt 2 to the provider chosen by headroom; the review runs on a provider other than the implementer and never on Auto.
2. Claude disabled and headroom known: the choice follows headroom (not only priority).
3. No headroom data: order is `providers.priority` exactly.
4. A `hard_worker` task's attempt 1 is not Auto.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
