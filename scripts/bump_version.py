"""Bump the harness package version for a release."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile


VERSION_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"(?=\r?$)', re.MULTILINE)
VERSION_FILE = Path(__file__).resolve().parents[1] / "harness" / "__init__.py"


def bump_version(version_file: Path, part: str) -> str:
    """Bump *part* in version_file and return the new semantic version."""
    original = version_file.read_bytes().decode("utf-8")
    matches = list(VERSION_RE.finditer(original))
    if len(matches) != 1:
        raise ValueError(
            f'{version_file} must contain exactly one __version__ = "X.Y.Z" line'
        )

    match = matches[0]
    major, minor, patch = (int(value) for value in match.groups())
    if part == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    new_version = f"{major}.{minor}.{patch}"
    updated = original[: match.start()] + f'__version__ = "{new_version}"' + original[match.end() :]

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=version_file.parent, delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, version_file.stat().st_mode)
        os.replace(temporary_name, version_file)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)

    return new_version


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump the harness package version.")
    parser.add_argument("--part", choices=("minor", "patch"), default="minor")
    args = parser.parse_args()

    try:
        new_version = bump_version(VERSION_FILE, args.part)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
