---
id: S2-T1
stage: 1
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/**
  - tests/harness/**
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
  - >-
    python -c "import pathlib,sys;bad=[str(p) for p in pathlib.Path('harness').rglob('*.py') if 'harness.py' in p.read_text(encoding='utf-8')];sys.exit('still mentions harness.py: '+', '.join(bad) if bad else 0)"
---

# Goal

The harness is now installed as the `harness` command; the old `harness.py` launcher no longer exists. Every message, hint and suggested command the harness prints must use `harness <subcommand>` instead of `python harness.py <subcommand>`.

# Scope

1. Replace every `python harness.py` and `harness.py` in strings under `harness/` with `harness` (for example `python harness.py resume S1` becomes `harness resume S1`).
2. Update tests that assert the old text.

# Out of scope

Any behavior change. README and other docs (already updated).

# Acceptance criteria

1. No file under `harness/` contains `harness.py`.
2. All tests pass; the only test changes are expected-text updates.
