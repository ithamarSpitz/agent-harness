"""Guard: machine-wide harness files are always resolved through harness_home()."""

from __future__ import annotations

from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parents[2] / "harness"
ALLOWED = {"home.py"}


def find_violations(directory: Path) -> list[str]:
    """Return 'file:line: text' for home-based '.harness' constructions."""
    found: list[str] = []
    for path in sorted(directory.glob("*.py")):
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if ".harness" not in line:
                continue
            if "Path.home()" in line or 'expanduser("~' in line or "expanduser('~" in line:
                found.append(f"{path.name}:{number}: {line.strip()}")
    return found


def test_no_module_builds_home_harness_path_directly() -> None:
    violations = find_violations(HARNESS_DIR)
    assert not violations, (
        "Use harness_home() from harness/home.py instead of building the path:\n"
        + "\n".join(violations)
    )


def test_detector_flags_direct_construction(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "from pathlib import Path\n\nx = Path.home() / \".harness\" / \"x\"\n",
        encoding="utf-8",
    )
    (tmp_path / "ok.py").write_text(
        "root = Path('.')\ny = root / \".harness\"\n", encoding="utf-8"
    )
    violations = find_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].startswith("bad.py:3:")
