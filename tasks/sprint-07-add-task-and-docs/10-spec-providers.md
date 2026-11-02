---
id: S7-T10
stage: 6
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S7-T6
  - S7-T8
allowed_paths:
  - docs/spec/providers.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;t=pathlib.Path('docs/spec/providers.md').read_text(encoding='utf-8');need=['## Providers and fallback','## Quota and capacity'];miss=[h for h in need if h not in t];sys.exit('missing: '+', '.join(miss) if miss else 0)"
---

# Goal

Finish the `## Providers and fallback` section of `docs/spec/providers.md` (one part of the harness specification; `SPEC.md` links it). The file already exists in your worktree from earlier attempts and is mostly correct. The `## Quota and capacity` section and everything after it belongs to task S7-T14: keep it exactly as it is (do not edit, move or delete it).

# Scope

Codex and Cursor routing, per-task and per-phase fallback, model classes and escalation, the implementation attempt budget, stall fallback.

# Findings from the last review (fix both)

1. Implementation attempt budget, live Cursor cap: the text says each Cursor index maps to `retry.sequence[i]` or repeats the last entry. The code is `retry.sequence or [task.model_class]`, then `sequence[min(index, len-1)]` (`_cursor_model_class` in `harness/provider_runner.py`). The default `retry.sequence` is empty (`RetryConfig` in `harness/config.py`), so the live class is the task's `model_class`. The budget is `max(1, len(retry.sequence))`, so with a non-empty sequence the last-entry clamp is never reached. Make this bullet agree with the code and with the phase table in the same file.
2. Per-phase stall fallback: after three timeouts, `fallback_after_stalls` may set the per-phase disable, then `_handle_agent_wait` still pauses with `_pause_for_capacity` and the task shows `WAITING_TIMEOUT` (`harness/scheduler.py`). The other provider runs only on the next `run`/`resume` (with `capacity.policy: wait`, after `retry_interval_minutes`); `waiting_provider` stays the stalled provider. `tests/harness/test_quota_plan_repair.py::test_review_timeouts_preserve_attempt_and_fallback_after_three` shows it. Unlike quota fallback, this is not a same-call continue: say so.

# How to write it

- Check every sentence against the code in `harness/`. When the code and the text disagree, the code wins.
- Describe what the code does today. Behavior that looks like a bug goes in the `## Known gaps` section at the end, described as it actually behaves.
- Where Codex and Cursor behave differently, say so explicitly.
- Plain, precise English, short sections, tables where useful. No history.

# Acceptance criteria

1. The required headings exist (checked by the verification command).
2. Every command, status, config key and default named exists in the code with that name and value.
3. No sentence in your section describes behavior the code does not have.
