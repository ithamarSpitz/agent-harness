---
id: S8-T1
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/memory.py
  - harness/config.py
  - tests/harness/test_memory.py
  - tests/harness/test_config.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Free memory reader and config keys -->
# Goal

A function that returns the machine's free memory, and the config keys that later tasks use to decide how many agents run at once.

# Decisions (do not reopen)

1. `harness/memory.py`: `free_memory_gb() -> float | None`.
   - Windows: `GlobalMemoryStatusEx` through `ctypes`, field `ullAvailPhys`.
   - Linux: `MemAvailable` from `/proc/meminfo`.
   - macOS: `vm_stat` output, (free + inactive + speculative pages) × page size from its header.
   - Any failure returns `None` (never raises).
   - The platform readers are separate functions so tests can call each with sample input (sample `/proc/meminfo` text, sample `vm_stat` text, a fake ctypes call).
2. `ExecutionConfig` in `harness/config.py`: `max_parallel_agents` keeps its default and meaning for now. New keys `min_free_memory_gb: float = 2.0` and `memory_per_agent_gb: float = 1.5`. Existing `harness.yaml` files load unchanged; negative or zero values are rejected with a clear config error.

# Acceptance criteria

1. Each platform parser returns the right value for a fixed sample and `None` for malformed input.
2. `free_memory_gb()` returns `None` when the platform reader raises.
3. Config loads with and without the new keys; invalid values fail with a message naming the key.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
