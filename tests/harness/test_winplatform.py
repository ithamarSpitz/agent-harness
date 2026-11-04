from __future__ import annotations

import platform
import sys
import threading
import time
from typing import Any

import pytest

from harness.winplatform import disable_wmi_queries


def test_disable_twice_is_fine() -> None:
    disable_wmi_queries()
    disable_wmi_queries()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_platform_calls_do_not_block_on_wmi(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*_a: object, **_k: object) -> Any:
        time.sleep(3600)

    monkeypatch.setattr(platform, "_wmi_query", hang, raising=False)
    disable_wmi_queries()
    monkeypatch.setattr(platform, "_uname_cache", None, raising=False)
    if hasattr(platform, "_processor_cache"):
        monkeypatch.setattr(platform, "_processor_cache", None, raising=False)

    results: dict[str, object] = {}

    def run() -> None:
        results["platform"] = platform.platform()
        results["machine"] = platform.machine()
        results["processor"] = platform.processor()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert all(isinstance(v, str) for v in results.values())
    assert len(results) == 3
