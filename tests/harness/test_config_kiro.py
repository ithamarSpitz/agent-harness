from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import HarnessConfig, KiroConfig


def load_config(tmp_path: Path, text: str, *, machine_path: Path | None = None) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(text, encoding="utf-8")
    return HarnessConfig.load(path, machine_path=machine_path)


def test_kiro_defaults_when_section_is_omitted(tmp_path: Path) -> None:
    config = load_config(tmp_path, "execution: {}\n")

    assert config.kiro == KiroConfig()
    assert config.kiro.enabled is False
    assert config.kiro.command == "kiro-cli"
    assert config.kiro.implementation_sequence == [
        "glm-5",
        "claude-sonnet-4.5",
        "claude-sonnet-4.5",
    ]
    assert config.kiro.review_model == "claude-sonnet-4.5"
    assert config.kiro.escalation_model == "claude-sonnet-4.5"
    assert config.kiro.quota_command == [
        "kiro-cli",
        "chat",
        "--no-interactive",
        "/usage",
    ]


def test_full_kiro_section_loads_every_key(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        """
kiro:
  enabled: true
  command: custom-kiro
  implementation_sequence: [model-a, model-b]
  review_model: model-review
  escalation_model: model-escalation
  quota_command: [custom-kiro, chat, /usage]
""",
    )

    assert config.kiro == KiroConfig(
        enabled=True,
        command="custom-kiro",
        implementation_sequence=["model-a", "model-b"],
        review_model="model-review",
        escalation_model="model-escalation",
        quota_command=["custom-kiro", "chat", "/usage"],
    )


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("kiro: []\n", r"kiro must be a mapping"),
        ("kiro: null\n", r"kiro must be a mapping"),
        ('kiro:\n  enabled: "yes"\n', r"kiro.enabled"),
        ("kiro:\n  command: ''\n", r"kiro.command"),
        ("kiro:\n  review_model: ''\n", r"kiro.review_model"),
        ("kiro:\n  escalation_model: ''\n", r"kiro.escalation_model"),
        ("kiro:\n  implementation_sequence: []\n", r"kiro.implementation_sequence"),
        ("kiro:\n  implementation_sequence: [model, '']\n", r"kiro.implementation_sequence\[1\]"),
        ("kiro:\n  quota_command: []\n", r"kiro.quota_command"),
        ("kiro:\n  quota_command: [kiro, 1]\n", r"kiro.quota_command\[1\]"),
        ("kiro:\n  review_model: auto\n", r"kiro.review_model must not be auto"),
        ("kiro:\n  unknown: value\n", r"kiro.unknown is not a known key"),
    ],
)
def test_kiro_rejects_invalid_values(
    tmp_path: Path, yaml_text: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path, yaml_text)


def test_machine_kiro_enabled_survives_project_without_section(tmp_path: Path) -> None:
    machine = tmp_path / "machine.yaml"
    machine.write_text("kiro:\n  enabled: true\n", encoding="utf-8")

    config = load_config(tmp_path, "execution: {}\n", machine_path=machine)

    assert config.kiro.enabled is True
