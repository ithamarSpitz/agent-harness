from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_log(log_dir: Path, name: str, content: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text(content, encoding="utf-8")
    return path
