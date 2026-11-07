"""S3: target-side TUI start (candidate B), pre-commit cleanup and residue list.

Single machine with a fake target: the move channel is scripted, the target
CLI's ps payload is scripted, and the teardown ports are doubles.  Nothing here
needs a real tmux or a second host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.p2.orchestration import target_tui as api
from hyprial.daemon.impl.transfer.p2.source.session import (
    DualActiveRiskWindow,
    SessionRollbackRequest,
)


class FakeRemote:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def run_json(self, argv: list[str], stdin: bytes | None = None) -> dict:
        self.calls.append(list(argv))
        if not self.responses:
            raise AssertionError(f"unexpected remote call {argv!r}")
        return self.responses.pop(0)


def _request(harness: str = "claude", platform: str = "") -> api.TuiStartRequest:
    return api.TuiStartRequest("claude" if harness == "claude" else harness, "worker", "s-1", platform=platform)


def test_start_argv_drives_the_target_cli_with_resume_and_tmux() -> None:
    assert api.start_argv(_request()) == [
        "agent",
        "start",
        "claude",
        "--name",
        "worker",
        "--resume",
        "s-1",
        "--tmux",
    ]
    assert api.start_argv(
        api.TuiStartRequest("pi", "worker", "s-1", cwd="/work")
    ) == [
        "agent",
        "start",
        "pi",
        "--name",
        "worker",
        "--resume",
        "s-1",
        "--tmux",
        "--cwd",
        "/work",
    ]


def test_start_argv_refuses_an_unsupported_tui_harness() -> None:
    with pytest.raises(api.TuiStartError) as refusal:
        api.start_argv(api.TuiStartRequest("codex", "worker", "s-1"))
    assert refusal.value.code == "TRANSFER_UNSUPPORTED_HARNESS"


def test_drive_target_start_verifies_claude_channel_confirmed() -> None:
    remote = FakeRemote(
        [
            {"ok": True, "tmuxSession": "hyprial-worker"},
            {
                "interactiveSessions": [
                    {
                        "actor": "worker",
                        "sessionRef": "s-1",
                        "channelConfirmed": True,
                    }
                ]
            },
        ]
    )
    result = api.drive_target_start(_request(), remote=remote)

    assert remote.calls[0] == api.start_argv(_request())
    assert remote.calls[1] == ["daemon", "status"]
    assert result.tmux_session == "hyprial-worker"
    assert set(result.attach) == {"macOS-iTerm2", "wsl-linux-terminal"}


def test_drive_target_start_verifies_pi_session_register() -> None:
    remote = FakeRemote(
        [
            {"ok": True, "tmuxSession": "hyprial-worker"},
            {
                "interactiveSessions": [
                    {
                        "actor": "worker",
                        "sessionRef": "s-1",
                        "source": "pi-extension",
                    }
                ]
            },
        ]
    )
    result = api.drive_target_start(_request("pi"), remote=remote)
    assert result.tmux_session == "hyprial-worker"


def test_drive_target_start_refuses_an_unregistered_session() -> None:
    remote = FakeRemote(
        [
            {"ok": True, "tmuxSession": "hyprial-worker"},
            {"interactiveSessions": [{"actor": "worker", "sessionRef": "s-1"}]},
        ]
    )
    with pytest.raises(api.TuiStartError) as refusal:
        api.drive_target_start(_request(), remote=remote)
    assert refusal.value.code == "CHANNEL_REGISTRATION_FAILED"
    assert refusal.value.data["tmuxSession"] == "hyprial-worker"


def test_drive_target_start_requires_a_tmux_session() -> None:
    remote = FakeRemote([{"ok": True}])
    with pytest.raises(api.TuiStartError) as refusal:
        api.drive_target_start(_request(), remote=remote)
    assert refusal.value.code == "TRANSFER_PROTOCOL"


def test_a_wsl_target_gets_only_the_plain_attach_line() -> None:
    remote = FakeRemote(
        [
            {"ok": True, "tmuxSession": "hyprial-worker"},
            {"interactiveSessions": [{"actor": "worker", "sessionRef": "s-1", "channelConfirmed": True}]},
        ]
    )
    result = api.drive_target_start(_request(platform="wsl"), remote=remote)
    assert result.attach == {"wsl-linux-terminal": "tmux attach -t hyprial-worker"}


def test_registration_confirmed_matches_a_canonical_actor_uri() -> None:
    status = {
        "interactiveSessions": [
            {
                "actor": "agent:alice:server:worker",
                "sessionRef": "s-1",
                "channelConfirmed": True,
            }
        ]
    }
    assert api.registration_confirmed("claude", "worker", "s-1", status) is True


def test_registration_confirmed_ignores_a_malformed_ps_payload() -> None:
    assert api.registration_confirmed("claude", "worker", "s-1", {}) is False
    assert (
        api.registration_confirmed(
            "claude", "worker", "s-1", {"interactiveSessions": "nope"}
        )
        is False
    )
    assert (
        api.registration_confirmed(
            "claude", "worker", "s-1", {"interactiveSessions": ["nope", 5, None]}
        )
        is False
    )


# ---------------------------------------------------------------------------
# pre-commit cleanup: the three residue classes
# ---------------------------------------------------------------------------


class FakePorts:
    def __init__(self, *, fail: tuple[str, ...] = ()) -> None:
        self.fail = set(fail)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} teardown refused")

    def kill_tmux(self, name: str) -> None:
        self.calls.append(("tmux", (name,)))
        self._maybe_fail("tmux")

    def kill_process(self, identity: str) -> None:
        self.calls.append(("process", (identity,)))
        self._maybe_fail("process")

    def undo_land(self, actor: str, token: str) -> None:
        self.calls.append(("landed", (actor, token)))
        self._maybe_fail("landed")


def _residue() -> api.TargetResidue:
    return api.TargetResidue(
        tmux_session="hyprial-worker",
        process_identity="proc:42",
        landed_actor="worker",
        entity_token="incarnation-1",
    )


def test_cleanup_target_clears_all_three_classes() -> None:
    ports = FakePorts()
    report = api.cleanup_target(_residue(), ports=ports)

    assert (report.tmux, report.process, report.landed) == (
        "cleared",
        "cleared",
        "cleared",
    )
    assert report.residual == ()
    assert ports.calls == [
        ("tmux", ("hyprial-worker",)),
        ("process", ("proc:42",)),
        ("landed", ("worker", "incarnation-1")),
    ]


def test_cleanup_target_names_each_residual_it_could_not_clear() -> None:
    ports = FakePorts(fail=("tmux", "process", "landed"))
    report = api.cleanup_target(_residue(), ports=ports)

    assert report.residual == ("tmux", "process", "landed")
    assert len(report.detail) == 3
    assert report.to_json()["residual"] == ["tmux", "process", "landed"]


def test_cleanup_target_marks_absent_classes_explicitly() -> None:
    ports = FakePorts()
    report = api.cleanup_target(
        api.TargetResidue(tmux_session="hyprial-worker"), ports=ports
    )

    assert (report.tmux, report.process, report.landed) == (
        "cleared",
        "absent",
        "absent",
    )
    assert ports.calls == [("tmux", ("hyprial-worker",))]


def test_cleanup_target_surfaces_a_half_landed_residue() -> None:
    """One half of a landed record is residual, never hidden as absent."""

    ports = FakePorts()
    actor_only = api.cleanup_target(api.TargetResidue(landed_actor="worker"), ports=ports)
    token_only = api.cleanup_target(api.TargetResidue(entity_token="incarnation-1"), ports=ports)

    assert actor_only.landed == api.RESIDUAL
    assert token_only.landed == api.RESIDUAL
    # undo_land needs both halves, so the port is never called with one.
    assert ports.calls == []
    assert any("landed" in item for item in actor_only.detail)


class FakeSourceTmux:
    def has_session(self, name: str) -> bool:
        return False


def test_rollback_pre_commit_restarts_the_source_and_clears_the_target() -> None:
    started: list[tuple] = []
    target_ports = FakePorts()
    report = api.rollback_pre_commit(
        source_request=SessionRollbackRequest(
            "op-1",
            "worker",
            "claude",
            "s-1",
            Path("/work"),
            {},
            ("claude",),
            "hyprial-worker",
        ),
        source_tmux=FakeSourceTmux(),
        source_tmux_bin="/usr/bin/tmux",
        target_residue=_residue(),
        target_ports=target_ports,
        launch=lambda *args, **kwargs: started.append((args, kwargs)),
    )

    assert report.source_restarted is True
    assert report.source_error is None
    assert report.target.residual == ()
    assert len(started) == 1


def test_rollback_pre_commit_post_s7_reports_only_and_leaves_the_target_in_place() -> None:
    """Post-S7 is report-only: the target is a committed result, not a residue."""

    target_ports = FakePorts()
    report = api.rollback_pre_commit(
        source_request=SessionRollbackRequest(
            "op-1",
            "worker",
            "claude",
            "s-1",
            Path("/work"),
            {},
            ("claude",),
            "hyprial-worker",
            target_started=True,
        ),
        source_tmux=FakeSourceTmux(),
        source_tmux_bin="/usr/bin/tmux",
        target_residue=_residue(),
        target_ports=target_ports,
        launch=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a post-S7 rollback must not restart the source")
        ),
    )

    assert report.source_restarted is False
    assert "DualActiveRiskWindow" in (report.source_error or "")
    # Neither the source restart nor any target teardown port ran.
    assert target_ports.calls == []
    assert (report.target.tmux, report.target.process, report.target.landed) == (
        api.LEFT_IN_PLACE,
        api.LEFT_IN_PLACE,
        api.LEFT_IN_PLACE,
    )
    assert report.target.residual == ("tmux", "process", "landed")
    assert report.to_json()["sourceRestarted"] is False


def test_post_s7_rollback_never_calls_the_target_teardown_ports() -> None:
    """The window can also be named explicitly; the three ports stay untouched."""

    target_ports = FakePorts()
    report = api.rollback_pre_commit(
        source_request=SessionRollbackRequest(
            "op-1", "worker", "claude", "s-1", Path("/work"), {}, ("claude",), "hyprial-worker"
        ),
        source_tmux=FakeSourceTmux(),
        source_tmux_bin="/usr/bin/tmux",
        target_residue=_residue(),
        target_ports=target_ports,
        target_started=True,
        launch=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a post-S7 rollback must not restart the source")
        ),
    )

    assert target_ports.calls == []
    assert report.target.residual == ("tmux", "process", "landed")


def test_dual_active_window_is_the_s2_error() -> None:
    """The refusal named here is the same S2 class the rollback reports."""

    assert isinstance(DualActiveRiskWindow("x"), RuntimeError)
