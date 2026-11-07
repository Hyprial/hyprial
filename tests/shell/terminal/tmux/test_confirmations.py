"""tui_tmux: confirmations scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
import json
from pathlib import Path
from typing import Any
import pytest
from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_mod

from .support import (
    _DEV_CHANNEL_PROMPT,
    _FOLDER_TRUST_PROMPT,
    _install_fake_daemon,
    _install_fake_tmux,
    _invoke_tmux_start,
    _legacy_runtime_context as _legacy_runtime_context,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support


def test_claude_confirmations_are_recognised_only_by_their_own_words() -> None:
    assert tmux_mod.claude_confirmation_prompt(_DEV_CHANNEL_PROMPT) == "development-channels"
    assert tmux_mod.claude_confirmation_prompt(_FOLDER_TRUST_PROMPT) == "folder-trust"
    assert tmux_mod.claude_confirmation_prompt("> ready for input\n") is None
    assert tmux_mod.claude_confirmation_prompt("") is None


def test_pane_text_is_empty_when_tmux_cannot_answer(monkeypatch: Any) -> None:
    def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(tmux_mod, "_run", missing)
    assert tmux_mod.pane_text("/nope/tmux", "s") == ""


@pytest.mark.parametrize(
    ("pane", "prompt"),
    [(_DEV_CHANNEL_PROMPT, "development-channels"), (_FOLDER_TRUST_PROMPT, "folder-trust")],
)
def test_start_claude_tmux_waiting_on_a_confirmation_keeps_the_pane(
    monkeypatch: Any, tmp_path: Path, pane: str, prompt: str
) -> None:
    """The prompt nobody in a detached pane can answer: say so, keep the pane.

    The old path waited out the deadline, killed the session, cleaned its
    launch config and reported CHANNEL_REGISTRATION_FAILED -- destroying the
    one pane a person could have confirmed in.
    """

    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed, registers=False)
    kill_record = _install_fake_tmux(monkeypatch, observed)
    monkeypatch.setattr(tmux_mod, "pane_text", lambda _bin, _name: pane)

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "CLAUDE_CONFIRMATION_REQUIRED"
    assert payload["data"]["prompt"] == prompt
    assert "tmux attach -t hyprial-agent-op-node-1-worker-node" in payload["error"]
    assert payload["data"]["tmuxSession"] == "hyprial-agent-op-node-1-worker-node"
    assert payload["data"]["sessionRef"] == observed["session_ref"]
    assert kill_record["killed"] == []
    # The TUI still needs its launch config after the person confirms.
    assert observed["config_path"].exists()


def test_start_claude_tmux_ordinary_pane_text_does_not_trip_the_detector(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Control: registration after a normal-looking pane is a normal success."""

    observed: dict[str, Any] = {}
    polls = {"n": 0}

    def daemon_request(method: str, params: Any = None, *, timeout: float = 15.0) -> dict[str, Any]:
        response: dict[str, Any] = {"daemon": {"running": True, "nodeId": "node-1", "owner": "op"}}
        if observed.get("spawned"):
            polls["n"] += 1
            if polls["n"] > 3:
                response["interactiveSessions"] = [{
                    "actor": "agent:op:node-1:worker-node",
                    "sessionRef": observed["session_ref"],
                    "channelConfirmed": True,
                }]
        return response

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    kill_record = _install_fake_tmux(monkeypatch, observed)
    monkeypatch.setattr(tmux_mod, "pane_text", lambda _bin, _name: "> ready for input\n")

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["detached"] is True
    assert kill_record["killed"] == []
