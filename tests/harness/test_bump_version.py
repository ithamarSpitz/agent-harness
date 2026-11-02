from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bump_version.py"


def make_release_tree(tmp_path: Path, version_line: str) -> tuple[Path, Path]:
    script = tmp_path / "scripts" / "bump_version.py"
    script.parent.mkdir()
    shutil.copyfile(SCRIPT, script)
    version_file = tmp_path / "harness" / "__init__.py"
    version_file.parent.mkdir()
    version_file.write_text(f'"""Agent harness package."""\n\n{version_line}\n', encoding="utf-8")
    return script, version_file


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], "0.17.0"),
        (["--part", "minor"], "0.17.0"),
        (["--part", "patch"], "0.16.1"),
    ],
)
def test_bumps_version_on_temp_copy(tmp_path: Path, arguments: list[str], expected: str) -> None:
    script, version_file = make_release_tree(tmp_path, '__version__ = "0.16.0"')

    result = subprocess.run(
        [sys.executable, str(script), *arguments],
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == expected
    assert f'__version__ = "{expected}"' in version_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "version_line",
    [
        "# version is intentionally missing",
        '__version__ = "0.16"',
        '__version__ = "0.16.0-dev"',
    ],
)
def test_invalid_version_exits_without_changing_file(tmp_path: Path, version_line: str) -> None:
    script, version_file = make_release_tree(tmp_path, version_line)
    original = version_file.read_bytes()

    result = subprocess.run(
        [sys.executable, str(script)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert '__version__ = "X.Y.Z"' in result.stderr
    assert version_file.read_bytes() == original
