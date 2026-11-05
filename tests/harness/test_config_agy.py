from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import AgyConfig, HarnessConfig


def load_config(tmp_path: Path, text: str) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(text, encoding="utf-8")
    return HarnessConfig.load(path)


def test_agy_defaults_when_section_is_omitted(tmp_path: Path):
    config = load_config(tmp_path, "execution: {}\n")

    assert config.agy == AgyConfig()
    assert config.agy.enabled is False
    assert config.agy.implementation_sequence == [
        {"model": "gemini-3.8-flash-medium"},
        {"model": "gemini-3.8-flash-high"},
        {"model": "claude-opus-4-6-thinking", "effort": "high"},
    ]
    assert config.agy.effort_models == ["gemini-"]
    assert config.agy.pools == {
        "gemini-weekly": ["gemini-"],
        "3p-weekly": ["claude-", "gpt-oss-"],
    }
    assert config.agy.base_allow == [
        "command(git status*)",
        "command(git diff*)",
        "command(git log*)",
        "command(graphify *)",
    ]


def test_full_agy_section_loads_every_key(tmp_path: Path):
    config = load_config(
        tmp_path,
        """
agy:
  enabled: true
  command: custom-agy
  implementation_sequence:
    - {model: first-model, effort: low}
    - {model: second-model}
  review_model: review-model
  review_effort: xhigh
  escalation_model: escalation-model
  escalation_effort: max
  effort_models: [first-, second-]
  pools:
    first-pool: [first-, review-]
    second-pool: [second-, escalation-]
  base_allow: [command(git status*), command(python -m pytest*)]
""",
    )

    assert config.agy == AgyConfig(
        enabled=True,
        command="custom-agy",
        implementation_sequence=[
            {"model": "first-model", "effort": "low"},
            {"model": "second-model"},
        ],
        review_model="review-model",
        review_effort="xhigh",
        escalation_model="escalation-model",
        escalation_effort="max",
        effort_models=["first-", "second-"],
        pools={
            "first-pool": ["first-", "review-"],
            "second-pool": ["second-", "escalation-"],
        },
        base_allow=["command(git status*)", "command(python -m pytest*)"],
    )


@pytest.mark.parametrize(
    ("yaml_text", "key"),
    [
        (
            "agy:\n  implementation_sequence: []\n",
            r"agy\.implementation_sequence",
        ),
        (
            "agy:\n  implementation_sequence: [{model: gemini-test, effort: extreme}]\n",
            r"agy\.implementation_sequence\[0\]\.effort",
        ),
        (
            "agy:\n  implementation_sequence: [{model: gemini-test, effort: [high]}]\n",
            r"agy\.implementation_sequence\[0\]\.effort",
        ),
        (
            "agy:\n  implementation_sequence: [{model: [gemini-test]}]\n",
            r"agy\.implementation_sequence\[0\]\.model",
        ),
        ("agy:\n  review_model: [claude-sonnet-4-6]\n", r"agy\.review_model"),
        ("agy:\n  escalation_effort: [high]\n", r"agy\.escalation_effort"),
        ("agy:\n  review_effort: extreme\n", r"agy\.review_effort"),
        ("agy:\n  effort_models: gemini-\n", r"agy\.effort_models"),
        ("agy:\n  effort_models: [gemini-, 1]\n", r"agy\.effort_models\[1\]"),
        ("agy:\n  base_allow: [[command]]\n", r"agy\.base_allow\[0\]"),
        (
            "agy:\n  pools:\n    gemini-weekly: [[gemini-]]\n",
            r"agy\.pools\.gemini-weekly\[0\]",
        ),
    ],
)
def test_agy_rejects_invalid_values(
    tmp_path: Path,
    yaml_text: str,
    key: str,
):
    with pytest.raises(ValueError, match=key):
        load_config(tmp_path, yaml_text)


def test_agy_nullable_efforts_load(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        "agy:\n"
        "  implementation_sequence: [{model: gemini-test, effort: null}]\n"
        "  review_effort: null\n"
        "  escalation_effort: null\n",
    )

    assert config.agy.implementation_sequence[0]["effort"] is None
    assert config.agy.review_effort is None
    assert config.agy.escalation_effort is None


@pytest.mark.parametrize(
    "yaml_text",
    [
        "agy:\n  implementation_sequence: [{model: unknown-model}]\n",
        "agy:\n  review_model: unknown-model\n",
        "agy:\n  escalation_model: unknown-model\n",
        "agy:\n  pools:\n    one: [gemini-]\n    two: [gemini-3.8-]\n",
    ],
)
def test_agy_rejects_models_without_exactly_one_pool(
    tmp_path: Path,
    yaml_text: str,
):
    with pytest.raises(ValueError, match=r"agy model .* exactly one pool"):
        load_config(tmp_path, yaml_text)


def test_provider_priority_accepts_agy(tmp_path: Path):
    config = load_config(
        tmp_path,
        "providers:\n  priority: [codex, cursor, claude, agy]\n",
    )

    assert config.providers.priority == ["codex", "cursor", "claude", "agy"]
