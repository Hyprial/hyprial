"""Upgrade fixture keeps both legacy null and current explicit summaries exact."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from contract.e2e._upgrade_continuity import inbox, installation


@pytest.mark.parametrize("help_text,expected", [("--summary TEXT", ["--summary", "hello"]), ("legacy send help", [])])
def test_old_send_summary_option_matches_help(help_text: str, expected: list[str]) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[1:] == ["message", "send", "--help"]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(argv, 0, help_text, "")

    assert installation.message_summary_args(
        {"subprocess": type("Processes", (), {"run": staticmethod(run)})},
        Path("/tmp/hr-upgrade-summary/bin/hyprial"), {}, "hello",
    ) == expected


def test_summary_probe_failure_does_not_silently_choose_legacy() -> None:
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv[1:])
        return subprocess.CompletedProcess(argv, 2, "", "bad help")

    with pytest.raises(AssertionError, match="message send help failed"):
        installation.message_summary_args(
            {"subprocess": type("Processes", (), {"run": staticmethod(run)})},
            Path("/tmp/hr-upgrade-summary/bin/hyprial"), {}, "hello",
        )
    assert calls == [["message", "send", "--help"]]


@pytest.mark.parametrize("help_text,expected", [("--summary TEXT", ["--summary", "hello"]), ("Usage: hyprial send", [])])
def test_pre_grouping_cli_uses_its_real_top_level_send_help(
    help_text: str, expected: list[str],
) -> None:
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv[1:])
        if argv[1:] == ["message", "send", "--help"]:
            return subprocess.CompletedProcess(argv, 2, "", "No such command 'message'.")
        assert argv[1:] == ["send", "--help"]
        return subprocess.CompletedProcess(argv, 0, help_text, "")

    assert installation.message_summary_args(
        {"subprocess": type("Processes", (), {"run": staticmethod(run)})},
        Path("/tmp/hr-upgrade-summary/bin/hyprial"), {}, "hello",
    ) == expected
    assert calls == [["message", "send", "--help"], ["send", "--help"]]


@pytest.mark.parametrize("help_text,expected", [
    ("--service / --no-service", ["init", "--no-service", "--json"]),
    ("--listen TEXT --json", ["init", "--json"]),
])
def test_init_avoids_service_registration_when_the_installed_cli_supports_it(
    help_text: str, expected: list[str],
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[1:] == ["init", "--help"]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(argv, 0, help_text, "")

    assert installation.init_command(
        {"subprocess": type("Processes", (), {"run": staticmethod(run)})},
        Path("/tmp/hr-upgrade-summary/bin/hyprial"), {},
    ) == expected


def test_failed_init_help_does_not_assume_a_legacy_safe_surface() -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "nested import failed")

    with pytest.raises(AssertionError, match="init help failed"):
        installation.init_command(
            {"subprocess": type("Processes", (), {"run": staticmethod(run)})},
            Path("/tmp/hr-upgrade-summary/bin/hyprial"), {},
        )


@pytest.mark.parametrize("summary", [None, "hello"])
def test_old_row_summary_is_preserved_exactly(summary: str | None) -> None:
    body = "hello"
    row = {"messageId": "m1", "summary": summary, "kind": "message", "bodyBytes": len(body)}

    def ipc(state: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "message.pending.list":
            return {"messages": [row]}
        if method == "session.register":
            return {"registered": True}
        assert method == "message.read_body"
        return {"message": body}

    entry = {"ipc": ipc, "uuid4": lambda: "fake-ref", "sys": sys, "Path": Path}
    assert inbox.assert_legacy_rows(entry, Path("/tmp/hr-upgrade-summary/state"), "driver", {"m1": body}, summaries={"m1": summary}) == {"m1": row}
    with pytest.raises(AssertionError, match="summary"):
        inbox.assert_legacy_rows(entry, Path("/tmp/hr-upgrade-summary/state"), "driver", {"m1": body}, summaries={"m1": "changed"})


def test_legacy_default_still_requires_null_summary() -> None:
    def ipc(state: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "message.pending.list":
            return {"messages": [{"messageId": "m1", "summary": "unexpected", "kind": "message", "bodyBytes": 5}]}
        return {"registered": True}

    with pytest.raises(AssertionError, match="summary"):
        inbox.assert_legacy_rows({"ipc": ipc, "uuid4": lambda: "fake-ref", "sys": sys, "Path": Path}, Path("/tmp/hr-upgrade-summary/state"), "driver", {"m1": "hello"})
