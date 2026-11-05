from __future__ import annotations

import json
import logging

import pytest

from harness.agy_permissions import build_settings


def allow_list(settings: str) -> list[str]:
    return json.loads(settings)["permissions"]["allow"]


def test_build_settings_adds_verification_base_and_extra_permissions() -> None:
    settings = build_settings(
        ["python -m pytest -q tests/harness", "npm test && npm run lint"],
        ["command(git status)"],
        ["command(playwright test)"],
    )

    assert allow_list(settings) == [
        "command(python -m pytest -q tests/harness)",
        "command(npm test)",
        "command(npm run lint)",
        "command(git status)",
        "command(playwright test)",
    ]


def test_splits_each_supported_shell_operator() -> None:
    settings = build_settings(
        ["ruff check harness | tee lint.txt; mypy harness && pytest -q"],
        [],
    )

    assert allow_list(settings) == [
        "command(ruff check harness)",
        "command(tee lint.txt)",
        "command(mypy harness)",
        "command(pytest -q)",
    ]


def test_review_settings_contain_only_base_permissions() -> None:
    settings = build_settings([], ["command(git status)", "command(git diff)"])

    assert allow_list(settings) == ["command(git status)", "command(git diff)"]


def test_permissions_are_deduplicated_in_first_seen_order() -> None:
    settings = build_settings(
        ["npm test && npm test", "python -m pytest"],
        ["command(npm test)", "command(git diff)", "command(git diff)"],
        ["command(python -m pytest)", "command(extra check)"],
    )

    assert allow_list(settings) == [
        "command(npm test)",
        "command(python -m pytest)",
        "command(git diff)",
        "command(extra check)",
    ]


DEFAULT_BASE_ALLOW = [
    "command(git status*)",
    "command(git diff*)",
    "command(git log*)",
    "command(git status)",
    "command(git status --short)",
    "command(git log --oneline -n 3)",
    "command(git log -n 3 --oneline)",
    "command(git log --oneline -20)",
    "command(git diff --stat)",
    "command(git show --stat HEAD)",
    "command(graphify *)",
]


def assert_no_wildcard_or_regex(settings: str) -> None:
    rules = allow_list(settings)
    assert "*" not in settings
    assert "regex:" not in settings.lower()
    assert all(rule.startswith("command(") for rule in rules)


def test_default_base_wildcards_become_exact_prompt_commands(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="harness.agy_permissions"):
        settings = build_settings([], DEFAULT_BASE_ALLOW)

    assert allow_list(settings) == [
        "command(git status)",
        "command(git diff)",
        "command(git diff --stat)",
        "command(git log --oneline -20)",
        "command(git status --short)",
        "command(git log --oneline -n 3)",
        "command(git log -n 3 --oneline)",
        "command(git show --stat HEAD)",
    ]
    assert_no_wildcard_or_regex(settings)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "command(graphify *)" in warnings[0].getMessage()


def test_base_sha_adds_exact_diff_rules() -> None:
    settings = build_settings([], DEFAULT_BASE_ALLOW, base_sha="deadbeef")

    assert allow_list(settings) == [
        "command(git status)",
        "command(git diff)",
        "command(git diff --stat)",
        "command(git log --oneline -20)",
        "command(git status --short)",
        "command(git log --oneline -n 3)",
        "command(git log -n 3 --oneline)",
        "command(git show --stat HEAD)",
        "command(git diff deadbeef)",
        "command(git diff deadbeef --stat)",
        "command(git diff --stat deadbeef)",
    ]
    assert_no_wildcard_or_regex(settings)


def test_no_base_sha_adds_no_sha_rules() -> None:
    assert allow_list(build_settings([], [], base_sha="  ")) == []
    assert allow_list(build_settings([], [])) == []


def test_unknowable_rules_from_any_source_are_dropped_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="harness.agy_permissions"):
        settings = build_settings(
            ["pytest tests/*.py && npm test", "graphify update ."],
            ["command(foo*)", "command(git status)", "regex:^git .*$"],
            [
                "command(graphify query callers --budget 500)",
                "REGEX:npm",
                "command(npm run lint)",
            ],
        )

    assert allow_list(settings) == [
        "command(npm test)",
        "command(git status)",
        "command(npm run lint)",
    ]
    assert_no_wildcard_or_regex(settings)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    for dropped in (
        "command(pytest tests/*.py)",
        "command(graphify update .)",
        "command(foo*)",
        "regex:^git .*$",
        "command(graphify query callers --budget 500)",
        "REGEX:npm",
    ):
        assert dropped in message


def test_no_warning_when_nothing_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="harness.agy_permissions"):
        build_settings(["npm test"], ["command(git status)"])

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_expanded_base_permissions_are_deduplicated_stably() -> None:
    settings = build_settings(
        [],
        ["command(git status*)", "command(git status)", "command(git status*)"],
        ["command(git diff abc123)"],
        base_sha="abc123",
    )

    assert allow_list(settings) == [
        "command(git status)",
        "command(git diff abc123)",
        "command(git diff abc123 --stat)",
        "command(git diff --stat abc123)",
    ]


def test_operators_inside_quotes_do_not_split_the_command() -> None:
    settings = build_settings(
        [
            'python -c "x=1;y=2" && npm test',
            "python -c 'import sys; print(1 | 2)' | tee out.txt",
            'echo "a && b" ; echo \'c;d\'',
        ],
        [],
    )

    assert allow_list(settings) == [
        'command(python -c "x=1;y=2")',
        "command(npm test)",
        "command(python -c 'import sys; print(1 | 2)')",
        "command(tee out.txt)",
        'command(echo "a && b")',
        "command(echo 'c;d')",
    ]


def test_escaped_operators_do_not_split_the_command() -> None:
    settings = build_settings([r'find . -exec echo {} \; && echo "x\"; y"'], [])

    assert allow_list(settings) == [
        r"command(find . -exec echo {} \;)",
        r'command(echo "x\"; y")',
    ]


def test_quoted_python_verification_with_many_semicolons_stays_whole() -> None:
    command = (
        'python -c "import subprocess,sys,time;d=[];'
        "[d.append((lambda t:(subprocess.run([sys.executable,'-m','pytest','-q',"
        "'tests/harness'],check=True),time.time()-t)[1])(time.time())) "
        "for _ in range(3)];print('three full runs, seconds:',"
        "[round(x,1) for x in d]);sys.exit('a run took 60s or more' "
        'if max(d)>=60 else 0)"'
    )

    settings = build_settings([command, "python -m ruff check harness tests"], [])

    assert allow_list(settings) == [
        f"command({command})",
        "command(python -m ruff check harness tests)",
    ]
