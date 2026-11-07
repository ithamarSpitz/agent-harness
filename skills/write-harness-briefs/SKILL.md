---
name: write-harness-briefs
description: Use when turning an approved feature spec and plan into validated harness sprint briefs and a roadmap entry.
---

# Write Harness Briefs

Turn a settled spec into small, executable task briefs. Preserve decisions from the
spec; do not invent a new design while decomposing it.

## Inputs

1. Read the supplied feature spec completely, then its plan if one exists. Spec Kit
   inputs are produced by the workflows under `.claude/skills/` and `.specify/`.
2. Inspect the code and tests that implement, call, or constrain the affected
   behavior. Search before guessing filenames, interfaces, or dependencies.
3. Read `SPEC.md` and only the relevant linked specification parts. Resolve no
   conflict silently: stop and report conflicts between the feature spec, plan,
   repository specification, and current implementation.
4. Use `tasks/sprint-11-field-fixes/14-weekly-cap-wait-time.md` and
   `tasks/sprint-12-agy-test-isolation/01-no-real-agy-in-tests.md` as examples of
   useful specificity, not as text templates.

Before writing, map every requirement and design decision to the code and tests it
needs. Record real paths and approximate line pointers from the current checkout.

## Brief file format

Create one Markdown file per task. Start with YAML frontmatter using these fields,
in this shape and order:

```yaml
---
id: SPRINT-TASK
stage: 1
model_class: worker
timeout_minutes: 30
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
```

Use values appropriate to the project and task, but include every field. Set
`allow_protected: true` only when the task intentionally needs a configured
protected path. `allowed_paths` is an exact edit boundary: include every source,
test, fixture, and documentation file the task may need to change. Never rely on an
agent editing outside allowed_paths.

After the frontmatter, write these sections in this order:

## Goal

State one observable outcome, not a list of implementation steps.

## Background

Explain the current behavior, evidence, and cause. Name relevant files with
approximate line pointers (for example, “around line 120”) so the implementer can
find the seam even when nearby code moves. Distinguish verified facts from
inferences.

## Decisions (do not reopen)

Use a numbered list for settled behavior, interfaces, defaults, edge cases, and
platform requirements. Include enough precision that separate task agents choose
compatible names and semantics. Do not prescribe irrelevant implementation detail.

## Acceptance criteria

Use a numbered list of independently checkable results. Name exact tests and
important inputs and outputs. Include regression coverage for behavior changed on
purpose. Every criterion must fit within the task's `allowed_paths` and
verification commands.

## Rules

State project constraints that matter to this task. Always include the scope stop
rule: change only `allowed_paths`; if work or a required fix falls outside
`allowed_paths`, stop and report it. Also say to stop and report when an existing
test outside allowed_paths fails and cannot be fixed within scope. Never weaken a
test to conceal a regression.

## Task sizing

- Give each task one coherent deliverable and one complete test cycle: add or
  update the tests, implement the behavior, and run its verification.
- Limit each task to at most 6 `allowed_paths` entries and at most 6 acceptance
  criteria.
- A fresh reviewer must be able to approve or reject the task independently.
- Keep tightly coupled code and tests together. Do not create separate setup or
  test-only tasks when they cannot deliver a usable result alone.
- If the work exceeds either limit or contains independently reviewable outcomes,
  split it into dependent tasks. Do not cut requirements or omit affected files to
  make a task appear smaller.

## Ordering

Build dependencies from actual produced/consumed interfaces. A task depends on
every earlier task whose behavior or interface it needs. Avoid dependencies added
only to force a preferred schedule.

Assign the same stage to tasks that may run side by side only when they have no
dependency relationship and touch different files. Tasks that share an
`allowed_paths` entry, consume another task's result, or must observe its merged
state belong in later stages with explicit `depends_on` entries. Check that task
IDs, stages, and dependencies form an acyclic order.

## Roadmap entry

Edit `tasks/roadmap.yaml` after the briefs are settled. Add the sprint name and one
row per task containing its brief file, stage, and `depends_on`. Keep the roadmap
values identical to the corresponding brief frontmatter. Order rows for a reader:
prerequisites before consumers, then task ID where either order is valid.

## Known-mistake checklist

Before validation, check every brief for all of these:

- **protected:** a protected path requires `allow_protected: true` and a clear
  reason in the brief.
- **missing-goal:** the Goal heading exists and names one observable outcome.
- **missing-criteria:** Acceptance criteria exists and has a numbered list.
- **stop-rule:** Rules explicitly says to stop for work or failures outside
  allowed_paths.
- **named-not-allowed:** every repository file named in Decisions or Acceptance
  criteria is covered by `allowed_paths`.
- **allowed-missing:** every non-glob allowed path exists, or a new file is named
  explicitly in the brief.
- **no-tests:** any task changing Python production code includes the affected
  tests in `allowed_paths`.
- **too-big:** there are no more than 6 paths or 6 criteria; split rather than
  hiding scope.
- Affected existing tests and fixtures are listed, not merely the new tests.
- A documentation task must not hide a code gap. Split each code gap into a code
  task.
- A live probe names a model pool that has quota.
- A documentation task depends on every code task whose behavior it documents.

## Sources

- `docs/references/brief-sources/superpowers-writing-plans.md` (Superpowers): task sizing, exact paths, and self-review ideas.
- `docs/references/brief-sources/speckit-tasks-template.md` (Spec Kit): dependency ordering and safe parallel-work ideas.
- `docs/references/brief-sources/` (Task Master's prompts): spec decomposition, complexity, dependency, and split ideas.

## Final validation

Run `harness validate <SPRINT>`. Fix every error and warning; if a warning must
remain, state exactly why it is correct and safe.

Run `harness brief-check <TASK>` for each task. Fix each brief and rerun the command
until it prints `OK`.
