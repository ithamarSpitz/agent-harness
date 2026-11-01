from __future__ import annotations

from pathlib import Path

from .config import HarnessConfig
from .models import TaskSpec


class ContextError(ValueError):
    pass


class ContextResolver:
    """Resolve small logical task context labels into repository files."""

    def __init__(self, root: Path, config: HarnessConfig):
        self.root = root.resolve()
        self.aliases = dict(config.context.aliases)

    def resolve_ref(self, ref: str) -> Path:
        raw_path = self.aliases.get(ref, ref)
        candidate = (self.root / raw_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ContextError(f"Context ref escapes repository: {ref} -> {raw_path}") from exc
        if not candidate.exists() or not candidate.is_file():
            if ref in self.aliases:
                raise ContextError(f"Context alias {ref!r} points to missing file: {raw_path}")
            raise ContextError(f"Context ref is neither a configured alias nor an existing file: {ref}")
        return candidate

    def resolve(self, task: TaskSpec) -> list[Path]:
        seen: set[Path] = set()
        result: list[Path] = []
        for ref in task.context_refs:
            path = self.resolve_ref(ref)
            if path not in seen:
                seen.add(path)
                result.append(path)
        return result

    def display_paths(self, task: TaskSpec) -> list[str]:
        return [path.relative_to(self.root).as_posix() for path in self.resolve(task)]
