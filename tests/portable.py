from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


def shell_join(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def python_command(code: str, *args: str | Path) -> str:
    return shell_join([sys.executable, "-c", code, *[str(arg) for arg in args]])


def file_exists_command(path: str | Path) -> str:
    return python_command(
        "import pathlib,sys; raise SystemExit(0 if pathlib.Path(sys.argv[1]).is_file() else 1)",
        path,
    )


def write_text_command(path: str | Path, text: str) -> str:
    return python_command(
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(sys.argv[2], encoding='utf-8')",
        path,
        text,
    )


def fail_command() -> str:
    return python_command("raise SystemExit(1)")


def yaml_quote(value: str | Path) -> str:
    # JSON strings are valid YAML scalars and safely escape Windows backslashes.
    return json.dumps(str(value))


def make_python_script(path: Path, body: str) -> Path:
    if path.suffix.lower() != ".py":
        path = path.with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path
