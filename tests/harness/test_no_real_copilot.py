from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_copilot_resolves_to_silent_failing_test_shim() -> None:
    executable = shutil.which("copilot")

    assert executable is not None
    assert Path(executable).parent.name.startswith("test-bin")
    result = subprocess.run(
        [executable],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""
