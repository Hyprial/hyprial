'Layer-1 protocol contract: a neutral fake session carrier, no terminal.\n\nThree-layer TUI test split (docs/notes/tui-test-layering.md, Allen\'s ruling):\n\n1. Protocol contract — register / lease / heartbeat / supersede / unregister /\n   generation confirm, exercised by a carrier that *speaks the same protocol*\n   but has no terminal, no stdio MCP child, and no Claude Code process.\n2. Real-TUI smoke — one thin "real claude starts, attaches, registers" path;\n   non-gating.\n3. Human experience — a manual checklist; never automated.\n\nThis file is layer 1. ``FakeCarrier`` drives the real ``DaemonApplication``\nthrough the exact IPC methods a production carrier uses\n(``session.register`` / ``session.refresh`` / ``session.heartbeat`` /\n``session.unregister`` and the fenced ``message.pending.list`` fetch), so a\nprotocol regression — e.g. the session-generation handover defect behind\n``agent ... already has a live connector`` in E2E-006/008/010/013 — goes red\nhere first, in milliseconds, with no PTY flake mixed in.\n\nWhat is deliberately NOT here: ``hyprial.mcp.channel`` (the Claude carrier\'s\nclient-side behavior — going quiet on SESSION_SUPERSEDED, reconnecting after a\ndaemon restart — is already pinned by tests/daemon/channels/mcp/lifecycle/test_mcp_channel_supersede.py and\ntests/daemon/channels/mcp/lifecycle/test_mcp_channel_restart.py). This file pins the *daemon-side* verdicts\nevery carrier of any source (claude-channel, pi-extension, codex-app-server)\nmust be able to rely on.\n'

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity.impl.agents.state.liveness import AGENT_HEARTBEAT_TTL_SECONDS, RUNTIME_HEADLESS
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION
from hyprial.kernel import session_fetch_params
from hyprial.daemon.impl.application import (
    DaemonApplication,
    DaemonRequestError,
    _LocalPresence,
)
from hyprial.daemon.impl.inbox import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    SubmissionResult,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

OWNER = "op"
NODE = "node-1"
CANONICAL = f"agent:{OWNER}:{NODE}:alice"
CWD = "/work/alice"


# --------------------------------------------------------------------------- #
# Minimal daemon doubles (same shape as tests/test_daemon_channel.py, kept
# local so this contract file stands alone)
# --------------------------------------------------------------------------- #
class FakeInbox:
    def __init__(self) -> None:
        self.messages: list[InboxMessage] = []
        self.fetches: list[str] = []

    def add(self, message_id: str, recipient: str = CANONICAL) -> None:
        self.messages.append(
            InboxMessage(
                message_id=message_id,
                conversation_id="conversation-1",
                sender="agent:bob",
                recipient=recipient,
                payload=json.dumps({"message": f"body of {message_id}"}).encode(),
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=1,
            )
        )

    def pending_messages(self, actor: str):  # type: ignore[no-untyped-def]
        return tuple(item for item in self.messages if item.recipient == actor)

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def pending_all(self):  # type: ignore[no-untyped-def]
        return tuple(self.messages)

    def fetch_pending(self, actor: str):  # type: ignore[no-untyped-def]
        self.fetches.append(actor)
        return self.pending_messages(actor)

    def submit(self, message: InboxMessage) -> SubmissionResult:
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class _NoWorkers:
    """Harness supervisor stand-in: no managed workers exist."""

    def status(self) -> tuple[()]:
        return ()


class _FakeLivelinessDirectory:
    """Raw token view: once declared, an actor is online forever (production
    `_ensure_interactive_route` semantics); only the heartbeat-gated
    `_LocalPresence` can tell a dead carrier apart from a live one."""

    def __init__(self) -> None:
        self._tokens: set[str] = set()

    def add(self, actor: str) -> None:
        self._tokens.add(actor)

    def actor_online(self, actor: str) -> bool:
        return actor in self._tokens

    def online_actors(self) -> tuple[str, ...]:
        return tuple(sorted(self._tokens))

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------- #
# The fake carrier: speaks the exact session wire protocol, owns no terminal
# --------------------------------------------------------------------------- #
class FakeCarrier:
    """One interactive session's carrier, reduced to its protocol surface.

    A production carrier (Claude Channel stdio child today; pi-extension and
    codex-app-server per the launcher design) does exactly four session-lifecycle
    calls plus fenced per-poll fetches. Everything else — PTY, MCP stdio,
    tmux — is transport, and transport belongs to layer 2.
    """

    def __init__(
        self,
        app: DaemonApplication,
        actor: str,
        session_ref: str,
        *,
        lease_token: str | None,
        source: str = "claude-channel",
        protocol_version: int | None = CHANNEL_PROTOCOL_VERSION,
    ) -> None:
        self.app = app
        self.actor = actor
        self.session_ref = session_ref
        self.lease_token = lease_token
        self.source = source
        self.protocol_version = protocol_version

    def register(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "actor": self.actor,
            "sessionRef": self.session_ref,
            "cwd": CWD,
            "command": ["fake-carrier"],
            "source": self.source,
            "runtime": f"{self.source.split('-', 1)[0]}_interactive",
        }
        if self.source == "claude-channel":
            params["channelConfirmed"] = True
            if self.protocol_version is not None:
                params["channelProtocolVersion"] = self.protocol_version
                params["channelLeaseToken"] = self.lease_token
        return self.app.handle("session.register", params)

    def refresh(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "actor": self.actor,
            "sessionRef": self.session_ref,
        }
        if self.lease_token is not None:
            params["channelLeaseToken"] = self.lease_token
        return self.app.handle("session.refresh", params)

    def heartbeat(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "actor": self.actor,
            "sessionRef": self.session_ref,
        }
        if self.lease_token is not None:
            params["channelLeaseToken"] = self.lease_token
        return self.app.handle("session.heartbeat", params)

    def unregister(self) -> dict[str, Any]:
        return self.app.handle(
            "session.unregister",
            {"actor": self.actor, "sessionRef": self.session_ref},
        )

    def fetch(self) -> dict[str, Any]:
        """The explicit carrier consumption boundary (fetched=true, fenced)."""

        return self.app.handle(
            "message.pending.list",
            {
                "actor": self.actor,
                "sessionRef": self.session_ref,
                **session_fetch_params(),
            },
        )


def error_code(call: Any) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    return str(caught.value.code)


class CarrierRig:
    """A daemon under test plus the wires a carrier contract needs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = FakeClock()
        self.inbox = FakeInbox()
        self.directory = _FakeLivelinessDirectory()
        self.apps: list[DaemonApplication] = []
        self.app = self._start_generation()

    def _start_generation(self) -> DaemonApplication:
        app = DaemonApplication(
            state_dir=self.root,
            socket_path=self.root / "daemon.sock",
            node_id=NODE,
            owner=OWNER,
        **_deep_module_daemon_dependencies())
        app._inbox = self.inbox  # type: ignore[assignment]
        app._harnesses = _NoWorkers()  # type: ignore[assignment]
        app._presence = _LocalPresence(  # type: ignore[assignment]
            self.directory,
            app.node_id,
            interactive_liveness=app._interactive_actor_liveness,
        )
        app._clock = self.clock  # type: ignore[assignment]
        self.apps.append(app)
        return app

    def close(self) -> None:
        while self.apps:
            self.apps.pop()._close()

    def restart_daemon(self) -> None:
        """A new daemon generation over the same state dir: fresh epoch,
        persisted desired state (interactive sessions) survives."""

        # A restart replaces the old runtime; leaving its actor threads and
        # SQLite handles alive models two daemons, not a new generation.
        self.close()
        self.app = self._start_generation()

    def carrier(
        self,
        session_ref: str,
        *,
        actor: str = "alice",
        source: str = "claude-channel",
    ) -> FakeCarrier:
        # Lease tokens must be opaque 32-256 character strings (daemon-side
        # validation), so derive a stable per-ref token of sufficient length.
        # Only claude-channel carriers hold a lease; every other carrier
        # source renews liveness through plain fenced contact.
        return FakeCarrier(
            self.app,
            actor,
            session_ref,
            lease_token=(
                f"lease-{session_ref}".ljust(32, "0")
                if source == "claude-channel"
                else None
            ),
            source=source,
        )

    def declare_route(self, actor: str = "alice") -> None:
        # Stand-in for _ensure_interactive_route declaring the raw token on
        # the daemon's own transport session (never exercised at this seam).
        self.directory.add(self.app._canonical_interactive_actor(actor))

    def ps_sessions(self) -> list[dict[str, Any]]:
        sessions = self.app.handle("ps", {})["interactiveSessions"]
        assert isinstance(sessions, list)
        return sessions


@pytest.fixture()
def rig(tmp_path: Path) -> CarrierRig:
    value = CarrierRig(tmp_path / "state")
    yield value
    value.close()


# --------------------------------------------------------------------------- #
# 1. Registration visibility
# --------------------------------------------------------------------------- #
def test_register_makes_exactly_one_session_visible(rig: CarrierRig) -> None:
    carrier = rig.carrier("session-1")
    result = carrier.register()
    assert result["ok"] is True
    assert result["actor"] == CANONICAL
    assert result["channelCurrentThisGeneration"] is True
    assert result["daemonEpoch"] == rig.app.epoch

    rig.declare_route()
    sessions = rig.ps_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session["actor"] == CANONICAL
    assert session["sessionRef"] == "session-1"
    assert session["source"] == "claude-channel"
    assert session["channelConfirmed"] is True
    assert session["online"] is True


# --------------------------------------------------------------------------- #
# 2. Supersede: last writer wins, the loser gets a distinct terminal verdict
# --------------------------------------------------------------------------- #
def test_supersede_gives_the_loser_a_distinct_terminal_verdict(
    rig: CarrierRig,
) -> None:
    old = rig.carrier("session-old")
    old.register()
    new = rig.carrier("session-new")
    new.register()

    # The new generation owns the actor...
    rig.declare_route()
    sessions = rig.ps_sessions()
    assert [item["sessionRef"] for item in sessions] == ["session-new"]

    # ...and every fenced call from the superseded carrier fails with the one
    # code that means "go quiet, never re-register" — distinct from both
    # STALE_SESSION (transient, daemon-restart self-heal) and
    # STALE_DAEMON_GENERATION (refresh first). A carrier's entire
    # supersede/reconnect decision tree is built on these three codes being
    # distinguishable.
    assert error_code(old.refresh) == "SESSION_SUPERSEDED"
    assert error_code(old.heartbeat) == "SESSION_SUPERSEDED"
    assert error_code(old.fetch) == "SESSION_SUPERSEDED"

    # The incumbent is unaffected by the loser's fenced calls.
    assert new.heartbeat()["alive"] is True
    assert new.fetch()["ok"] is True


def test_stale_session_is_distinct_from_superseded(rig: CarrierRig) -> None:
    carrier = rig.carrier("session-1")
    carrier.register()
    assert carrier.unregister()["unregistered"] is True
    # Nothing owns the actor now: not superseded, just gone — the verdict a
    # post-restart carrier must be allowed to self-heal from.
    assert error_code(carrier.fetch) == "STALE_SESSION"


# --------------------------------------------------------------------------- #
# 3. Daemon restart: generation confirm (refresh) before anything else
# --------------------------------------------------------------------------- #
def test_restart_requires_refresh_before_heartbeat(rig: CarrierRig) -> None:
    carrier = rig.carrier("session-1")
    carrier.register()

    rig.restart_daemon()
    # The carrier object still points at generation 1; rebind it to the new
    # daemon generation's handle, exactly like a surviving stdio child
    # reconnecting to the new daemon on the same socket.
    carrier.app = rig.app

    # Heartbeat before generation confirm is a protocol error, not silently
    # accepted: the new generation has no observation baseline for this lease.
    assert error_code(carrier.heartbeat) == "STALE_DAEMON_GENERATION"

    refreshed = carrier.refresh()
    assert refreshed["refreshed"] is True
    assert refreshed["channelCurrentThisGeneration"] is True
    assert refreshed["daemonEpoch"] == rig.app.epoch
    assert carrier.heartbeat()["alive"] is True
    assert carrier.fetch()["ok"] is True


def test_superseded_carrier_cannot_reclaim_after_daemon_restart(
    rig: CarrierRig,
) -> None:
    old = rig.carrier("session-old")
    old.register()
    new = rig.carrier("session-new")
    new.register()

    rig.restart_daemon()
    old.app = rig.app
    new.app = rig.app

    # Across a restart the supersede verdict survives (desired state is
    # persisted): the old generation's refresh is still terminal, and the
    # current generation can still confirm itself.
    assert error_code(old.refresh) == "SESSION_SUPERSEDED"
    assert new.refresh()["refreshed"] is True


def test_lease_token_guards_refresh_and_heartbeat(rig: CarrierRig) -> None:
    carrier = rig.carrier("session-1")
    carrier.register()
    impersonator = FakeCarrier(
        rig.app, "alice", "session-1", lease_token="lease-stolen-guess".ljust(32, "0")
    )
    assert error_code(impersonator.heartbeat) == "INVALID_CHANNEL_LEASE"
    assert error_code(impersonator.refresh) == "INVALID_CHANNEL_LEASE"
    # The legitimate carrier is unaffected.
    assert carrier.heartbeat()["alive"] is True


# --------------------------------------------------------------------------- #
# 4. Unregister is ref-guarded
# --------------------------------------------------------------------------- #
def test_unregister_with_a_stale_ref_cannot_kill_the_live_session(
    rig: CarrierRig,
) -> None:
    old = rig.carrier("session-old")
    old.register()
    new = rig.carrier("session-new")
    new.register()

    # A torn-down old generation (or a delayed finally block) must not be
    # able to unregister the session that replaced it.
    assert old.unregister()["unregistered"] is False
    rig.declare_route()
    sessions = rig.ps_sessions()
    assert [item["sessionRef"] for item in sessions] == ["session-new"]
    assert sessions[0]["online"] is True
    assert new.heartbeat()["alive"] is True

    # The current ref's unregister works, exactly once.
    assert new.unregister()["unregistered"] is True
    assert rig.ps_sessions() == []
    assert new.unregister()["unregistered"] is False


# --------------------------------------------------------------------------- #
# 5. Liveness handover: a dead generation must not hold the actor hostage
# --------------------------------------------------------------------------- #
def test_silent_generation_stops_reporting_online_and_frees_the_actor(
    rig: CarrierRig,
) -> None:
    """The protocol surface of today's E2E-008 red.

    The terminal layer died (PTY EIO) while the carrier's registration was
    still on record; the next launch then met `agent ... already has a live
    connector`. The daemon-side contract that makes this recoverable: liveness
    comes only from the session's own daemon contact, so a generation that
    goes silent ages out and a new register is never refused by a corpse.
    """

    first = rig.carrier("session-first")
    first.register()
    rig.declare_route()
    assert rig.ps_sessions()[0]["online"] is True

    # The generation goes silent (terminal died, carrier orphaned): no poll,
    # no heartbeat, no unregister.
    rig.clock.advance(AGENT_HEARTBEAT_TTL_SECONDS + 1.0)
    assert rig.ps_sessions()[0]["online"] is False

    # A new launch of the same actor must succeed against the dead
    # generation's leftover registration — last-writer supersede, not
    # AGENT_ALREADY_RUNNING.
    second = rig.carrier("session-second")
    assert second.register()["ok"] is True
    assert rig.ps_sessions()[0]["sessionRef"] == "session-second"
    assert rig.ps_sessions()[0]["online"] is True
    assert error_code(first.fetch) == "SESSION_SUPERSEDED"


def test_carrier_heartbeat_is_the_contact_that_keeps_liveness_alive(
    rig: CarrierRig,
) -> None:
    carrier = rig.carrier("session-1")
    carrier.register()
    rig.declare_route()
    rig.inbox.add("message-1")

    # Poll just under the TTL forever: liveness must never flap.
    for _ in range(5):
        rig.clock.advance(AGENT_HEARTBEAT_TTL_SECONDS - 1.0)
        carrier.heartbeat()
        fetched = carrier.fetch()
        assert [item["messageId"] for item in fetched["messages"]] == ["message-1"]
        assert rig.ps_sessions()[0]["online"] is True

    # Stop polling: the same session must age out.
    rig.clock.advance(AGENT_HEARTBEAT_TTL_SECONDS + 1.0)
    assert rig.ps_sessions()[0]["online"] is False


def test_live_headless_worker_refuses_interactive_takeover(
    rig: CarrierRig,
) -> None:
    """The other half of the one-connector invariant: `session.register`
    refuses only a *headless* incumbent (interactive-to-interactive is the
    supersede handoff above). Pin the refusal code so a TUI launch against a
    running worker fails with the actionable AGENT_ALREADY_RUNNING, and pin
    that a binding whose liveness cannot be judged counts as alive."""

    rig.app.handle("agent.create", {"name": "alice"})
    rig.app._agent_liveness.bind(
        CANONICAL,
        harness="claude",
        runtime=RUNTIME_HEADLESS,
        session_id="worker-session-1",
    )
    carrier = rig.carrier("session-tui")
    assert error_code(carrier.register) == "AGENT_ALREADY_RUNNING"

    # Once the worker is really gone (binding released, as `hyprial agent stop` does),
    # the same register succeeds — the remedy named in the error always works.
    rig.app._agent_liveness.release(CANONICAL)
    assert carrier.register()["ok"] is True


def test_time_monotonic_is_the_clock_the_contract_assumes(tmp_path: Path) -> None:
    """Guard rail for the FakeClock above: the production default is
    monotonic, so TTL math in this file models wall-clock silence, not
    suspend/restore artifacts."""

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    try:
        assert app._clock is time.monotonic
    finally:
        app._close()


# --------------------------------------------------------------------------- #
# 6. pi-extension: the lease-less carrier source speaks the same protocol
# --------------------------------------------------------------------------- #
def test_pi_extension_carrier_registers_visible_and_derives_harness(
    rig: CarrierRig,
) -> None:
    carrier = rig.carrier("session-pi-1", source="pi-extension")
    result = carrier.register()
    assert result["ok"] is True
    # Registering is the generation contact proof for every carrier source.
    assert result["channelCurrentThisGeneration"] is True

    rig.declare_route()
    (session,) = rig.ps_sessions()
    assert session["source"] == "pi-extension"
    assert session["runtime"] == "pi_interactive"
    assert session["sessionRef"] == "session-pi-1"
    assert session["online"] is True


def test_pi_extension_carrier_refresh_and_heartbeat_need_no_lease(
    rig: CarrierRig,
) -> None:
    carrier = rig.carrier("session-pi-1", source="pi-extension")
    carrier.register()
    rig.declare_route()
    # The same generation: refresh is a no-op confirm, heartbeat is alive.
    assert carrier.refresh()["refreshed"] is True
    assert carrier.heartbeat()["alive"] is True

    rig.restart_daemon()
    carrier.app = rig.app

    # Across a restart the generation proof is gone: heartbeat demands a
    # refresh first, exactly like the leased claude-channel carrier.
    assert error_code(carrier.heartbeat) == "STALE_DAEMON_GENERATION"
    assert carrier.refresh()["refreshed"] is True
    assert carrier.heartbeat()["alive"] is True
    assert carrier.fetch()["ok"] is True
    assert rig.ps_sessions()[0]["online"] is True


def test_pi_extension_supersede_verdicts_match_claude_channel(
    rig: CarrierRig,
) -> None:
    old = rig.carrier("session-pi-old", source="pi-extension")
    old.register()
    new = rig.carrier("session-pi-new", source="pi-extension")
    new.register()

    assert error_code(old.refresh) == "SESSION_SUPERSEDED"
    assert error_code(old.heartbeat) == "SESSION_SUPERSEDED"
    assert error_code(old.fetch) == "SESSION_SUPERSEDED"
    assert old.unregister()["unregistered"] is False
    assert new.heartbeat()["alive"] is True


def test_non_carrier_source_is_still_refused_refresh(rig: CarrierRig) -> None:
    carrier = FakeCarrier(
        rig.app, "alice", "session-1", lease_token=None, source="e2e"
    )
    carrier.register()
    assert error_code(carrier.refresh) == "INVALID_SESSION_SOURCE"
    assert error_code(carrier.heartbeat) == "INVALID_SESSION_SOURCE"


def test_lease_token_is_rejected_for_non_claude_carriers(rig: CarrierRig) -> None:
    carrier = FakeCarrier(
        rig.app,
        "alice",
        "session-1",
        lease_token="lease-not-allowed-here-00000000000000",
        source="pi-extension",
    )
    params: dict[str, Any] = {
        "actor": carrier.actor,
        "sessionRef": carrier.session_ref,
        "cwd": CWD,
        "command": ["fake-carrier"],
        "source": "pi-extension",
        "runtime": "pi_interactive",
        "channelLeaseToken": carrier.lease_token,
    }
    assert (
        error_code(lambda: rig.app.handle("session.register", params))
        == "INVALID_SESSION_SOURCE"
    )
