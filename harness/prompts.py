from __future__ import annotations

from .models import TaskSpec


def _context_block(context_paths: list[str]) -> str:
    if not context_paths:
        return "No harness-resolved context files are declared for this task. Follow the task brief's Required context section."
    return "Read these harness-resolved context files before coding:\n" + "\n".join(
        f"- {path}" for path in context_paths
    )


def implement_prompt(
    task: TaskSpec,
    previous_failure: str | None = None,
    context_paths: list[str] | None = None,
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

Follow the task scope exactly. Read only the declared/referenced context first; expand context only if a real dependency requires it.

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
) -> str:
    return f"""Review task {task.id} as an independent reviewer. Do not modify files.

Read:
- AGENTS.md
- {task.file.as_posix()}
{chr(10).join(f'- {path}' for path in (context_paths or []))}

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
"""


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
