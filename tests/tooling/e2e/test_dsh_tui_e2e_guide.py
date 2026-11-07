"""The dsh-tui E2E runner treats dsh-tui's first-run guide as a failure.

The workspace-start ruling (design ``dsh-tui-interactive-connector.md``
§10.15) makes the launcher own dsh-tui's first-run screens, so the real-PTY
runner must fail the moment the "Welcome to dsh-TUI" wizard shows on a fresh
HOME. An earlier runner dismissed the wizard with Escape and recorded
``dismissed after 1 Esc`` as an OK stage, which let a launcher seeding
regression pass the acceptance run (review-tui rejection, 2026-10-19).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "verify"))

import dsh_tui_e2e  # noqa: E402

WELCOME = "Welcome to dsh-TUI"


def _no_tmux(args: list[str], *, timeout: float = 30.0) -> object:
    raise AssertionError("the runner must not send keys to dismiss the guide")


def test_guide_showing_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[tuple[str, str]] = []

    def pane(socket_name: str, session: str) -> str:
        probes.append((socket_name, session))
        return f"header\n{WELCOME}\nEsc skip the guide"

    monkeypatch.setattr(dsh_tui_e2e, "pane", pane)
    monkeypatch.setattr(dsh_tui_e2e, "tmux", _no_tmux)
    evidence = dsh_tui_e2e.Evidence(None, "sock", "sess")

    with pytest.raises(dsh_tui_e2e.Failed, match="first-run guide"):
        dsh_tui_e2e.assert_no_first_run_guide(evidence, "sock", "sess")
    # One inspect plus at most the evidence snapshot: a dismissed-style retry
    # loop would keep probing until the wizard goes away.
    assert len(probes) <= 2


def test_no_guide_passes_without_sending_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dsh_tui_e2e, "pane", lambda socket_name, session: "workspace chat"
    )
    monkeypatch.setattr(dsh_tui_e2e, "tmux", _no_tmux)

    result = dsh_tui_e2e.assert_no_first_run_guide(
        dsh_tui_e2e.Evidence(None, "sock", "sess"), "sock", "sess"
    )

    assert result == "no guide"
