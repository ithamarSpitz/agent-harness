from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import HarnessConfig


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_project_overrides_machine_key_by_key(tmp_path: Path) -> None:
    machine = _write(tmp_path / "m.yaml", "copilot:\n  enabled: true\ncodex:\n  review_model: x\n")
    project = _write(tmp_path / "p.yaml", "codex:\n  review_model: y\n")
    cfg = HarnessConfig.load(project, machine_path=machine)
    assert cfg.copilot.enabled is True
    assert cfg.codex.review_model == "y"


def test_project_list_replaces_machine_list(tmp_path: Path) -> None:
    machine = _write(tmp_path / "m.yaml", "codex:\n  implementation_sequence: [a, b, c]\n")
    project = _write(tmp_path / "p.yaml", "codex:\n  implementation_sequence: [z]\n")
    cfg = HarnessConfig.load(project, machine_path=machine)
    assert cfg.codex.implementation_sequence == ["z"]


def test_missing_machine_file_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _write(tmp_path / "p.yaml", "codex:\n  review_model: y\n")
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "nohome"))
    assert HarnessConfig.load(project) == HarnessConfig.load(project, machine_path=tmp_path / "none.yaml")


def test_default_machine_file_is_harness_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write(home / "config.yaml", "codex:\n  review_model: m\n")
    project = _write(tmp_path / "p.yaml", "{}\n")
    monkeypatch.setenv("HARNESS_HOME", str(home))
    assert HarnessConfig.load(project).codex.review_model == "m"


def test_non_mapping_machine_file_raises(tmp_path: Path) -> None:
    machine = _write(tmp_path / "m.yaml", "- a\n")
    project = _write(tmp_path / "p.yaml", "{}\n")
    with pytest.raises(ValueError, match="m.yaml"):
        HarnessConfig.load(project, machine_path=machine)


@pytest.mark.parametrize("text", ["", "null\n", "# only a comment\n"])
def test_empty_machine_file_raises(tmp_path: Path, text: str) -> None:
    machine = _write(tmp_path / "m.yaml", text)
    project = _write(tmp_path / "p.yaml", "{}\n")
    with pytest.raises(ValueError, match="m.yaml"):
        HarnessConfig.load(project, machine_path=machine)


def test_invalid_yaml_machine_file_raises(tmp_path: Path) -> None:
    machine = _write(tmp_path / "m.yaml", "a: [unclosed\n")
    project = _write(tmp_path / "p.yaml", "{}\n")
    with pytest.raises(ValueError, match="m.yaml"):
        HarnessConfig.load(project, machine_path=machine)


@pytest.mark.parametrize("project_text", [None, "a: [unclosed\n"])
def test_machine_file_is_read_before_project_file(tmp_path: Path, project_text: str | None) -> None:
    machine = _write(tmp_path / "m.yaml", "- a\n")
    project = tmp_path / "p.yaml"
    if project_text is not None:
        _write(project, project_text)
    with pytest.raises(ValueError, match="m.yaml"):
        HarnessConfig.load(project, machine_path=machine)
