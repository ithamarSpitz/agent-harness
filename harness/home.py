from __future__ import annotations

import os
from pathlib import Path


def harness_home() -> Path:
    """Machine-wide harness folder: ``HARNESS_HOME`` or ``~/.harness``."""
    override = os.environ.get("HARNESS_HOME")
    if override:
        return Path(override)
    return Path.home() / ".harness"
