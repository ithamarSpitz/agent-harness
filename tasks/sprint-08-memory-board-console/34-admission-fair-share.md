---
id: S8-T34
stage: 2
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S8-T1
  - S8-T33
allowed_paths:
  - harness/admission.py
  - tests/harness/test_admission.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A pure decision function: may this harness start one more agent now, given free memory and what every harness on the computer is running and waiting for? It shares memory fairly between projects.

# Decisions (do not reopen)

1. `harness/admission.py`: `can_start(*, project, free_gb, estimate_gb, min_free_gb, max_parallel_agents, warmup_minutes, ledger, now) -> Admission(allowed: bool, reason: str)`. No I/O.
2. Memory not yet visible: an agent started less than `warmup_minutes` ago (default 3) has not grown yet, so its `estimate_gb` counts as reserved: `available = free_gb - reserved - min_free_gb`.
3. Rules, in order:
   - this project already runs `max_parallel_agents` → no;
   - this project runs no agent → yes (every project always gets one agent, so no project ever stalls; the reason says when memory is short);
   - `available < estimate_gb` → no;
   - fairness: if another project is waiting (has a waiting record with `ready_tasks > 0`) and runs fewer agents than this project, → no (this project yields to it);
   - otherwise → yes.
4. `reason` is one short line for logs and the board, for example `waiting: 1.8 GB available, 2.1 GB needed` or `waiting: booknook runs 1 agent and is waiting; this project runs 2`.

# Acceptance criteria

Table-driven tests:
1. Plenty of memory, one project → yes until `max_parallel_agents`.
2. A project with no agents → yes even with little memory.
3. A just-started agent's estimate is reserved; after `warmup_minutes` it is not.
4. Two projects, both waiting, A runs 2 and B runs 1, memory for one more → A no, B yes.
5. Every "no" has a reason.
