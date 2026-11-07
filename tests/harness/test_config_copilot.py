from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import CopilotConfig, HarnessConfig


def load_config(tmp_path: Path, text: str) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(text, encoding="utf-8")
    return HarnessConfig.load(path)


def test_copilot_defaults_when_section_is_omitted(tmp_path: Path) -> None:
    config = load_config(tmp_path, "execution: {}\n")

    assert config.copilot == CopilotConfig()
    assert config.copilot.enabled is False
    assert config.copilot.command == "copilot"
    assert config.copilot.implementation_sequence == [
        {"model": "auto", "auto_tier": "balance"},
        {"model": "auto", "auto_tier": "intelligence"},
        {"model": "auto", "auto_tier": "intelligence", "effort": "high"},
    ]
    assert config.copilot.review_effort is None
    assert config.copilot.quota_command == ["gh", "api", "copilot_internal/user"]


def test_full_copilot_section_loads_every_key(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        """
copilot:
  enabled: true
  command: custom-copilot
  implementation_sequence:
    - {model: auto, auto_tier: efficiency, effort: minimal}
    - {model: custom-model, effort: max}
  review_model: auto
  review_auto_tier: balance
  review_effort: low
  escalation_model: custom-escalation
  escalation_auto_tier: null
  escalation_effort: xhigh
  quota_command: [custom-gh, api, custom/copilot]
""",
    )

    assert config.copilot == CopilotConfig(
        enabled=True,
        command="custom-copilot",
        implementation_sequence=[
            {"model": "auto", "auto_tier": "efficiency", "effort": "minimal"},
            {"model": "custom-model", "effort": "max"},
        ],
        review_model="auto",
        review_auto_tier="balance",
        review_effort="low",
        escalation_model="custom-escalation",
        escalation_auto_tier=None,
        escalation_effort="xhigh",
        quota_command=["custom-gh", "api", "custom/copilot"],
    )


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (
            "copilot:\n  implementation_sequence: []\n",
            r"copilot\.implementation_sequence must not be empty",
        ),
        (
            "copilot:\n  implementation_sequence: "
            "[{model: auto, auto_tier: fastest}]\n",
            r"auto_tier value 'fastest'",
        ),
        (
            "copilot:\n  implementation_sequence: "
            "[{model: gpt-test, auto_tier: balance}]\n",
            r"auto_tier value 'balance'.*not 'gpt-test'",
        ),
        (
            "copilot:\n  implementation_sequence: "
            "[{model: auto, effort: extreme}]\n",
            r"effort value 'extreme'",
        ),
        (
            "copilot:\n  review_auto_tier: fastest\n",
            r"copilot\.review_auto_tier value 'fastest'",
        ),
        (
            "copilot:\n  escalation_effort: extreme\n",
            r"copilot\.escalation_effort value 'extreme'",
        ),
    ],
)
def test_copilot_rejects_invalid_values(
    tmp_path: Path,
    yaml_text: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path, yaml_text)


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        (
            "copilot:\n  implementation_sequence: [{model: [auto]}]\n",
            r"model value \['auto'\] must be a string",
        ),
        (
            "copilot:\n  implementation_sequence: "
            "[{model: custom, auto_tier: [balance]}]\n",
            r"auto_tier value \['balance'\] must be a string",
        ),
        (
            "copilot:\n  implementation_sequence: "
            "[{model: auto, effort: [high]}]\n",
            r"effort value \['high'\] must be a string",
        ),
        (
            "copilot:\n  review_model: [auto]\n",
            r"review_model value \['auto'\] must be a string",
        ),
        (
            "copilot:\n  review_auto_tier: [balance]\n",
            r"review_auto_tier value \['balance'\] must be a string",
        ),
        (
            "copilot:\n  escalation_effort: [high]\n",
            r"escalation_effort value \['high'\] must be a string",
        ),
    ],
)
def test_copilot_rejects_non_string_values_before_value_checks(
    tmp_path: Path,
    yaml_text: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path, yaml_text)


def test_provider_priority_accepts_copilot(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        "providers:\n  priority: [copilot, codex]\n",
    )

    assert config.providers.priority == ["copilot", "codex"]
