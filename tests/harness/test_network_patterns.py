from __future__ import annotations

from pathlib import Path

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.runner import CursorAgentRunner

ECONNREFUSED = "Error: [unavailable] connect ECONNREFUSED 192.168.16.1:443"
NGHTTP2 = (
    "RetriableError: [internal] Stream closed with error code NGHTTP2_REFUSED_STREAM"
)
RESOURCE_EXHAUSTED = "RetriableError: [resource_exhausted]"
MODEL_UNAVAILABLE = "model unavailable"


def _config(tmp_path: Path) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        """execution: {}
worktree: {}
code_map: {command: graphify}
cursor:
  command: agent
  models: {worker: test-model}
codex:
  enabled: true
  command: codex
verification: {}
paths: {}
context: {}
doctor: {}
""",
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def test_econnrefused_is_network_for_cursor_and_codex(tmp_path: Path):
    config = _config(tmp_path)
    cursor = CursorAgentRunner(config, tmp_path / "logs")
    codex = CodexAgentRunner(config, tmp_path / "logs")

    assert cursor._waiting_cause(ECONNREFUSED) == "network"
    assert codex._classify(ECONNREFUSED, timed_out=False) == "network"


def test_nghttp2_is_network_while_resource_exhausted_stays_busy(tmp_path: Path):
    config = _config(tmp_path)
    cursor = CursorAgentRunner(config, tmp_path / "logs")

    assert cursor._waiting_cause(NGHTTP2) == "network"
    assert cursor._waiting_cause(RESOURCE_EXHAUSTED) == "provider_busy"


def test_model_unavailable_is_not_network(tmp_path: Path):
    config = _config(tmp_path)
    cursor = CursorAgentRunner(config, tmp_path / "logs")
    codex = CodexAgentRunner(config, tmp_path / "logs")

    assert cursor._waiting_cause(MODEL_UNAVAILABLE) is None
    assert codex._classify(MODEL_UNAVAILABLE, timed_out=False) == "model_unavailable"
    assert "unavailable" not in {
        p.casefold() for p in config.capacity.network_patterns + config.codex.network_patterns
    }
    assert "retriableerror" not in {
        p.casefold() for p in config.capacity.network_patterns + config.codex.network_patterns
    }
