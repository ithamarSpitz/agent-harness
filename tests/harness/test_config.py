from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import ExecutionConfig, HarnessConfig


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_execution_defaults_without_new_keys(tmp_path: Path):
    write(tmp_path / "harness.yaml", "execution: {}\n")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.execution == ExecutionConfig()
    assert config.execution.min_free_memory_gb == 2.0
    assert config.execution.memory_per_agent_gb == 1.5


def test_execution_existing_yaml_keeps_max_parallel_and_memory_defaults(tmp_path: Path):
    write(tmp_path / "harness.yaml", "execution:\n  max_parallel_agents: 5\n")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.execution.max_parallel_agents == 5
    assert config.execution.min_free_memory_gb == 2.0
    assert config.execution.memory_per_agent_gb == 1.5


def test_execution_loads_explicit_memory_keys(tmp_path: Path):
    write(
        tmp_path / "harness.yaml",
        """
execution:
  min_free_memory_gb: 3.5
  memory_per_agent_gb: 2.25
""",
    )
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.execution.min_free_memory_gb == 3.5
    assert config.execution.memory_per_agent_gb == 2.25


@pytest.mark.parametrize(
    ("yaml_snippet", "message"),
    [
        ("min_free_memory_gb: 0", "execution.min_free_memory_gb"),
        ("min_free_memory_gb: -1", "execution.min_free_memory_gb"),
        ("memory_per_agent_gb: 0", "execution.memory_per_agent_gb"),
        ("memory_per_agent_gb: -0.5", "execution.memory_per_agent_gb"),
    ],
)
def test_execution_rejects_invalid_memory_values(
    tmp_path: Path,
    yaml_snippet: str,
    message: str,
):
    write(tmp_path / "harness.yaml", f"execution:\n  {yaml_snippet}\n")
    with pytest.raises(ValueError, match=message):
        HarnessConfig.load(tmp_path / "harness.yaml")
