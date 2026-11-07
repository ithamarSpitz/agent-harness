# Brief-writing sources

Copied 2026-11-07 as reference material for `skills/write-harness-briefs/SKILL.md`. Read them for ideas; the skill states our own rules.

| File | From | Commit | License |
| --- | --- | --- | --- |
| superpowers-writing-plans.md | [obra/superpowers](https://github.com/obra/superpowers) `skills/writing-plans/SKILL.md` | 22f0b463e7a5 | MIT |
| speckit-tasks-template.md | [github/spec-kit](https://github.com/github/spec-kit) `templates/tasks-template.md` | c43849408ac0 | MIT |
| taskmaster-parse-prd.json, taskmaster-analyze-complexity.json, taskmaster-expand-task.json | [eyaltoledano/claude-task-master](https://github.com/eyaltoledano/claude-task-master) `src/prompts/` | c0c98d367c55 | MIT with Commons Clause (use and change allowed, no resale) |

What each is used for:

- Superpowers writing-plans: task right-sizing, exact file paths, one checkable result per step, the self-review list.
- Spec Kit tasks template: marking tasks that can run in parallel (our stages) and ordering by dependency.
- Task Master prompts: turning a spec into ordered tasks with dependencies, scoring complexity, and when to split a task.
