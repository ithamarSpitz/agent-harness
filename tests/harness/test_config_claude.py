from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import ClaudeConfig, ClaudeReserveConfig, HarnessConfig


def load_config(tmp_path: Path, text: str) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(text, encoding="utf-8")
    return HarnessConfig.load(path)


def test_claude_defaults_when_section_is_omitted(tmp_path: Path):
    config = load_config(tmp_path, "execution: {}\n")

    assert config.claude == ClaudeConfig()
    assert config.claude.enabled is False
    assert config.claude.reserve == ClaudeReserveConfig(
        five_hour_max_used_percent=50,
        weekly_max_used_percent=70,
    )


def test_full_claude_section_loads_every_key(tmp_path: Path):
    config = load_config(
        tmp_path,
        """
claude:
  enabled: true
  command: custom-claude
  implementation_sequence:
    - {model: haiku, effort: low}
    - {model: sonnet, effort: max}
  review_model: review-model
  review_effort: medium
  escalation_model: escalation-model
  escalation_effort: max
  permission_mode: default
  reserve:
    five_hour_max_used_percent: 25
    weekly_max_used_percent: 60
""",
    )

    assert config.claude == ClaudeConfig(
        enabled=True,
        command="custom-claude",
        implementation_sequence=[
            {"model": "haiku", "effort": "low"},
            {"model": "sonnet", "effort": "max"},
        ],
        review_model="review-model",
        review_effort="medium",
        escalation_model="escalation-model",
        escalation_effort="max",
        permission_mode="default",
        reserve=ClaudeReserveConfig(
            five_hour_max_used_percent=25,
            weekly_max_used_percent=60,
        ),
    )


@pytest.mark.parametrize(
    ("yaml_text", "key"),
    [
        (
            "claude:\n  implementation_sequence: [{model: sonnet, effort: extreme}]\n",
            r"claude\.implementation_sequence\[0\]\.effort",
        ),
        (
            "claude:\n  implementation_sequence: [{model: sonnet, effort: [high]}]\n",
            r"claude\.implementation_sequence\[0\]\.effort",
        ),
        (
            "claude:\n  implementation_sequence: [{model: sonnet, effort: {level: high}}]\n",
            r"claude\.implementation_sequence\[0\]\.effort",
        ),
        ("claude:\n  review_effort: extreme\n", r"claude\.review_effort"),
        ("claude:\n  review_effort: [high]\n", r"claude\.review_effort"),
        ("claude:\n  review_effort: {level: high}\n", r"claude\.review_effort"),
        ("claude:\n  escalation_effort: extreme\n", r"claude\.escalation_effort"),
        ("claude:\n  escalation_effort: [high]\n", r"claude\.escalation_effort"),
        (
            "claude:\n  reserve:\n    five_hour_max_used_percent: -1\n",
            r"claude\.reserve\.five_hour_max_used_percent",
        ),
        (
            "claude:\n  reserve:\n    weekly_max_used_percent: 101\n",
            r"claude\.reserve\.weekly_max_used_percent",
        ),
        (
            "claude:\n  reserve:\n    weekly_max_used_percent: true\n",
            r"claude\.reserve\.weekly_max_used_percent",
        ),
    ],
)
def test_claude_rejects_invalid_effort_or_percent(
    tmp_path: Path,
    yaml_text: str,
    key: str,
):
    with pytest.raises(ValueError, match=key):
        load_config(tmp_path, yaml_text)


@pytest.mark.parametrize(
    "profile",
    [
        "{effort: high}",
        "{model: '', effort: high}",
        "{model: '   ', effort: high}",
        "{model: null, effort: high}",
        "{model: 123, effort: high}",
    ],
)
@pytest.mark.parametrize("index", [0, 1])
def test_claude_rejects_missing_or_invalid_sequence_model(
    tmp_path: Path, profile: str, index: int,
):
    prefix = "    - {model: sonnet, effort: medium}\n" * index
    with pytest.raises(
        ValueError, match=rf"claude\.implementation_sequence\[{index}\]\.model",
    ):
        load_config(
            tmp_path,
            f"claude:\n  implementation_sequence:\n{prefix}    - {profile}\n",
        )


def test_claude_rejects_empty_implementation_sequence(tmp_path: Path):
    with pytest.raises(ValueError, match=r"claude\.implementation_sequence"):
        load_config(tmp_path, "claude:\n  implementation_sequence: []\n")


def test_provider_priority_accepts_claude(tmp_path: Path):
    config = load_config(
        tmp_path,
        "providers:\n  priority: [codex, claude, cursor]\n",
    )

    assert config.providers.priority == ["codex", "claude", "cursor"]
