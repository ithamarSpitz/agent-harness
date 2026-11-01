from __future__ import annotations

from .config import UIReviewConfig
from .models import TaskSpec
from .process_utils import split_command


def _context_block(context_paths: list[str]) -> str:
    if not context_paths:
        return "No harness-resolved context files are declared for this task. Follow the task brief's Required context section."
    return "Read these harness-resolved context files before coding:\n" + "\n".join(
        f"- {path}" for path in context_paths
    )


def _code_map_block(offered: bool, command: str) -> str:
    if not offered:
        return ""
    parts = split_command(command)
    cmd = parts[0] if parts else "graphify"
    return (
        f"A code map of this repository is available in graphify-out/. You decide whether it helps. "
        f"If you use it, first read graphify-out/AGENTS-graphify.md. Add `--budget 500` to `{cmd} query`."
    )


def _code_map_section(offered: bool, command: str) -> str:
    block = _code_map_block(offered, command)
    return f"\n\n{block}" if block else ""


def _ui_review_section(ui_review: UIReviewConfig) -> str:
    commands = "\n".join(ui_review.start_commands)
    return f"""

UI check (your decision):
If the changes add or change behavior that can affect what a user sees or does in the UI, check it in a real browser:
1. Start the app in the background from the repository root:
{commands}
2. Open {ui_review.url} with the Playwright browser tools and try the changed behavior.
3. Do not change any files.
If the changes cannot affect the UI, do not start anything.
After the VERDICT line, add one line: `UI_CHECK: done - <what you tried and what you saw>` or `UI_CHECK: skipped - <why the changes cannot affect the UI>`.
A UI problem you saw is a FAIL finding.
"""


def implement_prompt(
    task: TaskSpec,
    previous_failure: str | None = None,
    context_paths: list[str] | None = None,
    code_map_offered: bool = False,
    code_map_command: str = "graphify",
) -> str:
    retry = ""
    if previous_failure:
        retry = f"""

This is a fresh retry context. The previous attempt failed for the following reason:

---
{previous_failure}
---

Inspect the current working tree, fix only the failure, and do not broaden scope.
"""
    return f"""You are executing task {task.id}.

First read AGENTS.md, then read the task brief at {task.file.as_posix()}.
{_context_block(context_paths or [])}

Follow the task scope exactly. Read only the declared/referenced context first; expand context only if a real dependency requires it.{_code_map_section(code_map_offered, code_map_command)}

Do not redesign the architecture. Do not work on future tasks. Do not commit changes; the harness owns commits and integration.

Run the verification commands listed in the task before finishing when possible.{retry}

Finish with a concise report containing:
STATUS
CHANGED_FILES
TEST_RESULTS
OPEN_ISSUES
"""


def review_prompt(
    task: TaskSpec,
    base_ref: str,
    verification_summary: str,
    context_paths: list[str] | None = None,
    code_map_offered: bool = False,
    code_map_command: str = "graphify",
    ui_review: UIReviewConfig | None = None,
) -> str:
    ui_section = _ui_review_section(ui_review) if ui_review is not None and ui_review.enabled else ""
    return f"""Review task {task.id} as an independent reviewer. Do not modify files.

Read:
- AGENTS.md
- {task.file.as_posix()}
{chr(10).join(f'- {path}' for path in (context_paths or []))}{_code_map_section(code_map_offered, code_map_command)}

Review the current working tree diff against base ref `{base_ref}`.

Mechanical verification already reported:
---
{verification_summary}
---

Check specifically for:
- missed acceptance criteria
- assignment/spec violations
- architecture invariant violations
- correctness or idempotency/concurrency edge cases relevant to this task
- tests that pass without actually proving the requested behavior
- accidental scope expansion

Your FIRST non-empty line must be exactly one of:
VERDICT: PASS
VERDICT: FAIL

If FAIL, list concrete findings with file paths and why they matter. Do not propose unrelated refactors.
{ui_section}"""


def plan_change_prompt(
    task: TaskSpec,
    failure_summary: str,
    context_paths: list[str] | None = None,
) -> str:
    return f"""You are acting as a read-only plan-repair analyst for task {task.id}. Do not modify files.

Read:
- AGENTS.md
- ARCHITECTURE.md
- {task.file.as_posix()}
{chr(10).join(f'- {path}' for path in (context_paths or []))}

The task exhausted its normal implementation path. Failure evidence:
---
{failure_summary}
---

Decide whether the problem is:
1. IMPLEMENTATION_ONLY: the existing architecture/task is sufficient; a stronger focused retry should solve it.
2. TASK_ADDITION: architecture remains valid, but one or more missing prerequisite/corrective tasks must be added.
3. ARCHITECTURE_CHANGE: the documented architecture itself must change before implementation can proceed.
4. UNKNOWN: evidence is insufficient.

Your FIRST non-empty line must be exactly one of:
CHANGE_KIND: IMPLEMENTATION_ONLY
CHANGE_KIND: TASK_ADDITION
CHANGE_KIND: ARCHITECTURE_CHANGE
CHANGE_KIND: UNKNOWN

Then provide concise sections:
EVIDENCE
WHY_CURRENT_PLAN_CANNOT_PROCEED (or why it can for IMPLEMENTATION_ONLY)
PROPOSED_CHANGE
PROPOSED_NEW_TASKS
AFFECTED_EXISTING_TASKS
REVALIDATION_REQUIRED
RISKS

Do not edit the architecture, roadmap, or code. Do not silently relax requirements. Cite file paths/sections from the repository when possible.
"""
