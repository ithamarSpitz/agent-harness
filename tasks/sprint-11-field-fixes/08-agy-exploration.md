---
id: S11-T8
stage: 3
model_class: hard_worker
timeout_minutes: 75
review: true
allow_protected: false
depends_on:
  - S11-T7
allowed_paths:
  - harness/agy_runner.py
  - harness/agy_permissions.py
  - harness/config.py
  - tests/harness/test_agy_runner.py
  - tests/harness/test_agy_permissions.py
  - tests/harness/test_config_agy.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- agy can look around the worktree and run read-only git (report items 11 and 13) -->
# Goal

agy implementation and review calls can explore the worktree and read git history without being denied, while writes stay limited to the worktree and the task's checks.

# Background

BookNook, 0.20.0: agy attempts ended in seconds with `denied_actions` for `git log -n 3 --oneline`, `git status`, and `Get-ChildItem -Path supabase/tests`. agy permission rules match exact commands only (`*` and `regex:` are not supported, S10-T3), so exploration commands with arguments can never all be listed. Every such attempt burns a task attempt. BookNook moved agy behind Codex and Claude as a workaround.

agy 1.5.0 options (from `agy --help`): `--mode accept-edits|auto|plan`, `--sandbox` ("terminal restrictions enabled"), `--dangerously-skip-permissions`, `--settings` (per-call JSON).

# Decisions (do not reopen)

1. **Find out first.** Run the real agy with `gemini-3.8-flash-low` or `-medium`: a few short calls in a temporary git repo outside this repo. For each of (a) `--mode accept-edits` with today's exact rules, (b) `--mode auto`, (c) `--sandbox` plus `--dangerously-skip-permissions`, record whether agy can: run `git log -n 3 --oneline`; run `Get-ChildItem` or `ls`; read a file; write a file inside the temp repo; write a file OUTSIDE the temp repo (use a second temp folder); reach the network (`curl https://example.com` or similar). Put the table in your final message and as a comment block above the command builder in `harness/agy_runner.py`, with the agy version.
2. **Choose by these rules, in order.** Use the first option that allows the read-only commands AND blocks writes outside the workspace. If (b) or (c) qualifies, use it for implementation calls; reviews stay in `plan` mode if `plan` allows the reads, otherwise use the same option. If none qualifies, keep (a) and do decisions 3 and 4 only.
3. **Prompt.** agy calls (implementation and review) get one prompt paragraph: use agy's own file tools to list, read and search files instead of shell commands; the base commit, the changed files and the diff summary are already in the prompt (add `git diff --stat <base>` output to the agy review prompt if it is not there yet); do not run `graphify`.
4. **Base allow list** (still used if (a) stays): add the exact forms `git status`, `git status --short`, `git log --oneline -n 3`, `git log -n 3 --oneline`, `git log --oneline -20`, `git diff --stat`, `git show --stat HEAD` to the defaults in `harness/config.py`.
5. Never use `--dangerously-skip-permissions` without `--sandbox`, and never if the check in decision 1 shows that writes outside the workspace succeed.

# Acceptance criteria

1. Command-line tests show the chosen flags for implementation and review.
2. A test checks the prompt paragraph from decision 3 is present for agy calls.
3. The final message contains the decision-1 table with the agy version.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
