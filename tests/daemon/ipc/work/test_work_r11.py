"""R11 (review 1126): the session.register process fence on real-shaped chains."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.application.actors.sessions import registry as session_registry
from hyprial.daemon.impl.mcp.channel.ownership import _process_identities_match
from hyprial.kernel import DaemonRequestError, ipc_errors

from .test_work_r8 import _r8_work_daemon  # noqa: F401 - registers the r8_work_daemon fixture
from .test_work_r8 import _carrier_registration

PEER = 300


def _register(app, parents: dict[int, int | None], claim: int, monkeypatch) -> None:
    app._ipc_peer.pid = PEER
    monkeypatch.setattr(session_registry, "_read_process_parent", parents.get)
    monkeypatch.setattr(session_registry, "process_birth_identity", lambda _pid: "claimed")
    app.handle(
        "session.register",
        _carrier_registration(claim, "claimed", f"r11-{claim}"),
        _trusted_message_origin="ipc",
    )


def test_ancestor_claim_above_an_unreadable_root_process_is_accepted(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude under Terminal.app: the claim's own ancestry hits root-owned
    /usr/bin/login (unreadable), but the peer's ancestry proves the claim."""

    app, _space_id = r8_work_daemon
    _register(app, {PEER: 200, 200: 50, 50: None}, 200, monkeypatch)

    assert [s.process_pid for s in app.desired_state.load().interactive_sessions] == [200]


def test_direct_child_claim_is_accepted_but_a_grandchild_is_not(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex: the IPC CLI Popen()s the claimed app-server (depth one only)."""

    app, _space_id = r8_work_daemon
    parents = {400: PEER, 500: 400, PEER: 1}
    _register(app, parents, 400, monkeypatch)
    with pytest.raises(DaemonRequestError) as refused:
        _register(app, parents, 500, monkeypatch)

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert [s.process_pid for s in app.desired_state.load().interactive_sessions] == [400]


@pytest.mark.parametrize(
    "parents",
    [
        {PEER: 301, 301: PEER},  # cycle
        {PEER + i: PEER + i + 1 for i in range(200)},  # over-long chain
    ],
    ids=["cycle", "over-long"],
)
def test_cycles_and_over_long_chains_are_not_proof(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch, parents
) -> None:
    app, _space_id = r8_work_daemon
    with pytest.raises(DaemonRequestError) as refused:
        _register(app, parents, 200, monkeypatch)

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert app.desired_state.load().interactive_sessions == ()


@pytest.mark.parametrize(
    ("expected", "observed", "matches"),
    [
        ("darwin-startsec:100", "darwin-startsec:101", True),  # only shared: tolerated
        ("darwin-startsec:100", "darwin-startsec:102", False),
        # A finer shared component differs: PID reuse, the coarse near-miss loses.
        ("darwin-startsec:100;ps-lstart:A", "darwin-startsec:101;ps-lstart:B", False),
        ("darwin-startsec:100;ps-lstart:A", "darwin-startsec:105;ps-lstart:A", True),
    ],
)
def test_darwin_second_tolerance_only_when_it_is_the_only_shared_component(
    expected: str, observed: str, matches: bool
) -> None:
    assert _process_identities_match(expected, observed) is matches
