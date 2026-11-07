"""``hyprial agent start claude -- --allowedTools <rule that does not do what it says>``.

Two Claude Code behaviours, both documented:

* A path rule for Write, MultiEdit, NotebookEdit or Glob never matches (only
  Edit(...) and Read(...) are checked).  The tool name proves it: always refused.
* In a CLI flag, ``Edit(/x/**)`` is VALID and anchors at the primary working
  directory; absolute needs ``//``.  Refusing it, or rewriting it to the absolute
  form, could break or silently widen a correct rule, so it is refused only when
  a root directory ``/x`` exists and ``<cwd>/x`` does not; if both exist, or the
  cwd is unknown, it is only a warning.

The check lives in the start CLI only: stored arguments replayed by restore or
wake never pass through it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.agent.runtime.rules.permission_rules import (
    inert_path_rules,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

REFUSAL = "would be accepted but would not do what they say"


@pytest.mark.parametrize(
    "args, expected",
    [
        # Never-matched tools: refused whatever their slashes; the suggestion keeps the path as written.
        (("--allowedTools", "Write(/tmp/x/**)"), [("Write(/tmp/x/**)", "Edit(/tmp/x/**)")]),
        (("--allowedTools", "Write(//tmp/x/**)"), [("Write(//tmp/x/**)", "Edit(//tmp/x/**)")]),
        (("--allowedTools", "Write(/no-such-root-dir/x/**)"), [("Write(/no-such-root-dir/x/**)", "Edit(/no-such-root-dir/x/**)")]),
        (("--allowedTools", "Write(docs/**)"), [("Write(docs/**)", "Edit(docs/**)")]),
        (("--disallowedTools", "Write(docs/**)"), [("Write(docs/**)", "Edit(docs/**)")]),
        (
            ("--allowedTools", "NotebookEdit(docs/n)", "MultiEdit(docs/m)"),
            [("NotebookEdit(docs/n)", "Edit(docs/n)"), ("MultiEdit(docs/m)", "Edit(docs/m)")],
        ),
        # Glob is covered by Read, not Edit.
        (("--allowedTools", "Glob(docs/**)"), [("Glob(docs/**)", "Read(docs/**)")]),
        (("--allowedTools", "Glob(/tmp/g/**)"), [("Glob(/tmp/g/**)", "Read(/tmp/g/**)")]),
        (("--allowed-tools=Write(docs/a)",), [("Write(docs/a)", "Edit(docs/a)")]),
        (
            ("--allowedTools", "Bash(git:*),Edit(docs/**),Write(docs/**)"),
            [("Write(docs/**)", "Edit(docs/**)")],
        ),
    ],
)
def test_never_matched_tools_are_refused(args, expected, tmp_path: Path) -> None:
    findings = inert_path_rules(args, tmp_path)
    assert findings.refuse == expected
    assert findings.warn == []


def test_single_slash_absolute_path_is_refused_when_the_cwd_has_no_such_directory(tmp_path: Path) -> None:
    findings = inert_path_rules(("--allowedTools", "Edit(/tmp/x/**),Read(/tmp/y)"), tmp_path)

    assert findings.refuse == [
        ("Edit(/tmp/x/**)", "Edit(//tmp/x/**)"),
        ("Read(/tmp/y)", "Read(//tmp/y)"),
    ]
    assert findings.warn == []


def test_single_slash_path_that_exists_under_the_cwd_is_only_a_warning(tmp_path: Path) -> None:
    (tmp_path / "tmp").mkdir()

    findings = inert_path_rules(("--allowedTools", "Edit(/tmp/x/**)"), tmp_path)

    assert findings.refuse == []
    assert findings.warn == [("Edit(/tmp/x/**)", "Edit(//tmp/x/**)")]


def test_single_slash_path_with_an_unknown_cwd_is_only_a_warning() -> None:
    findings = inert_path_rules(("--allowedTools", "Edit(/tmp/x/**)"), None)

    assert findings.refuse == []
    assert findings.warn == [("Edit(/tmp/x/**)", "Edit(//tmp/x/**)")]


@pytest.mark.parametrize(
    "args",
    [
        ("--allowedTools", "Edit(//tmp/x/**)"),
        ("--allowedTools", "Read(//tmp/x/**)"),
        ("--allowedTools", "Edit(~/x/**)"),
        ("--allowedTools", "Edit(/no-such-root-dir/**)"),
        ("--allowedTools", "Edit(/**/*.py)"),
        ("--allowedTools", "Edit(docs/**)"),
        ("--allowedTools", "Bash(git commit:*)"),
        # A bare tool name matches every use of the tool: the documented way to allow it.
        ("--allowedTools", "Write", "Glob", "NotebookEdit"),
        ("--model", "x", "Edit(/tmp/x/**)"),
        (),
    ],
)
def test_effective_or_unrelated_rules_are_left_alone(args, tmp_path: Path) -> None:
    findings = inert_path_rules(args, tmp_path)
    assert findings.refuse == [] and findings.warn == []


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, dict[str, object]]]:
    seen: list[tuple[str, dict[str, object]]] = []

    def daemon(method: str, params: dict[str, object] | None = None, **_: object) -> dict[str, object]:
        seen.append((method, dict(params or {})))
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    return seen


def _start(cwd: Path, rule: str):
    return CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start", "claude", "--name", "rule-probe", "--headless", "--json",
            "--cwd", str(cwd), "--", "--allowedTools", rule,
        ],
    )


@pytest.mark.parametrize(
    "rule, suggestion",
    [
        ("Edit(/tmp/x/**)", "Edit(/tmp/x/**) -> Edit(//tmp/x/**)"),
        ("Write(//tmp/x/**)", "Write(//tmp/x/**) -> Edit(//tmp/x/**)"),
        ("Glob(docs/**)", "Glob(docs/**) -> Read(docs/**)"),
    ],
)
def test_start_refuses_before_any_daemon_call(calls, tmp_path: Path, rule: str, suggestion: str) -> None:
    result = _start(tmp_path, rule)

    assert result.exit_code != 0
    assert REFUSAL in result.output
    assert suggestion in result.output
    assert "write only the tool name" in result.output
    assert "Write-only restriction cannot be expressed" in result.output
    assert calls == []


def test_start_does_not_refuse_a_single_slash_rule_the_cwd_can_resolve(calls, tmp_path: Path) -> None:
    (tmp_path / "tmp").mkdir()

    result = _start(tmp_path, "Edit(/tmp/x/**)")

    assert REFUSAL not in result.output
