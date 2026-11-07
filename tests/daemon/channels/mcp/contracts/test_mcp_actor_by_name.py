"""MCP/interactive sessions stop persisting derived actor addresses.

Three fixes, one incident (2026-09-04 production owner switch #352): a daemon
restart moved the interactive sessions to the new owner's address, but the
already-running claude Channel children kept announcing the OLD four-segment
URI they had been launched with, so every fenced call came back
SESSION_SUPERSEDED (a terminal verdict: go quiet, never re-register) and both
sessions lost inbound delivery until they were reopened by hand.

F1  ``hyprial agent start claude`` writes the BARE actor name into the mcp-config and
    the channel argv; the daemon mints the canonical URI per call from its own
    current owner/node, so a launch never persists a derived address.
F2  An owner-segment-only relocation (owner migration rewrote the stored
    address of the SAME session_ref) is not a supersede: the carrier keeps
    serving under the canonical spelling.
F3  A fetch rejected by the session fence commits nothing: no fetched stamp,
    no sender-outbox retirement, and the row stays pending for whichever
    session actually holds the actor.
"""

from __future__ import annotations

import json
import time
import uuid as uuid_module
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    RetryPolicy,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import subprocess as _cli_subprocess
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER_A = "op"
OWNER_B = "boss"
NODE = "node-1"
ACTOR = "alice"
CANONICAL_A = f"agent:{OWNER_A}:{NODE}:{ACTOR}"
CANONICAL_B = f"agent:{OWNER_B}:{NODE}:{ACTOR}"
SESSION_REF = "session-1"
SENDER = "channel:lark:test-app"


# --------------------------------------------------------------------------- #
# Daemon harness: a real InboxService over a real sqlite file, the same three
# process doubles the in-tree carrier-contract tests install.
# --------------------------------------------------------------------------- #
class _KeepOutboxTransport(MemoryDeliveryTransport):
    "Deliver into the recipient inbox but keep the sender's outbox hold.\n\n    Same double as tests/daemon/harnesses/interactive/test_interactive_session_fetch_contract.py\n    (CommitWithoutReceiptTransport): retirement then becomes an observable the\n    fetch boundary owns, which is exactly what F3 asserts.\n    "

    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        # Wall-clock now: reads hide rows past their TTL by the wall clock.
        self.service.receive(message, now_ms=time.time_ns() // 1_000_000)
        # Keep the original request in the reproduced split inbox/outbox state.
        return message.intent == "reply"


class _NoWorkers:
    def status(self) -> tuple[()]:
        return ()


class _EmptyPresence:
    def actor_online(self, actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class DaemonRig:
    """One daemon generation over a state dir, with a real durable inbox."""

    def __init__(self, root: Path, *, owner: str) -> None:
        self.root = root
        self.state = root / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        # DaemonApplication.__init__ runs migrate_owner_if_needed, exactly like
        # a daemon restart after `settings.json` owner changed.
        self.app = DaemonApplication(
            state_dir=self.state,
            socket_path=self.state / "daemon.sock",
            node_id=NODE,
            owner=owner,
            hyprial_home=self.home,
        **_deep_module_daemon_dependencies())
        self.app._harnesses = _NoWorkers()  # type: ignore[assignment]
        self.app._presence = _EmptyPresence()  # type: ignore[assignment]

    def attach_inbox(self, inbox: InboxService) -> None:
        self.app._inbox = inbox  # type: ignore[assignment]

    def register(self, actor: str, session_ref: str) -> Any:
        return self.app.handle(
            "session.register",
            {
                "actor": actor,
                "sessionRef": session_ref,
                "cwd": "/work",
                "command": ["claude"],
                "source": "claude-channel",
                "channelConfirmed": True,
            },
        )

    def fetch(self, actor: str, session_ref: str) -> Any:
        return self.app.handle(
            "message.pending.list",
            {"actor": actor, "sessionRef": session_ref, "fetched": True},
        )

    def close(self) -> None:
        self.app._agent_session_domains.close()
        self.app.agents.close()


def _inbox_service(root: Path) -> tuple[InboxService, _KeepOutboxTransport]:
    transport = _KeepOutboxTransport()
    inbox = InboxService(
        root / "state" / "inbox.sqlite3",
        transport,
        retry_policy=RetryPolicy(),
        interactive_recipient=lambda recipient: recipient.startswith("agent:"),
    )
    transport.service = inbox
    return inbox, transport


def _pending_message(message_id: str, recipient: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender=SENDER,
        recipient=recipient,
        payload=json.dumps({"message": f"body of {message_id}"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=time.time_ns() // 1_000_000,
    )


@pytest.fixture()
def inbox(tmp_path: Path) -> InboxService:
    service = _inbox_service(tmp_path)
    yield service
    service.close()


def _delivered(
    inbox: InboxService, transport: _KeepOutboxTransport, message_id: str, recipient: str
) -> None:
    message = _pending_message(message_id, recipient)
    assert inbox.submit(message, now_ms=message.created_at_ms).queued
    assert transport.deliver(message) is False  # request keeps the outbox hold


# --------------------------------------------------------------------------- #
# F1: the launch persists the bare name, never a derived URI
# --------------------------------------------------------------------------- #
def test_start_claude_config_and_argv_carry_the_bare_name(tmp_path: Path) -> None:
    """The mcp-config JSON and the channel argv contain the bare actor name.

    RED before F1: the config embedded ``--actor agent:op:node-1:worker-node``
    -- a derived address frozen at launch time, which is what stranded running
    channels on a dead owner segment after #352.
    """

    from hyprial.daemon.impl.mcp import channel as mcp_channel

    state_dir = tmp_path / "state"
    hyprial_home = tmp_path / "home"
    hyprial_home.mkdir()
    session_uuid = uuid_module.UUID("11111111-2222-3333-4444-555555555555")
    config_uuid = uuid_module.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    uuids = iter([session_uuid, config_uuid])
    monkey = pytest.MonkeyPatch()
    monkey.setattr(_cli_get_services(), "uuid4", lambda: next(uuids))
    monkey.setattr(
        mcp_channel, "_read_process_identity", lambda _pid: "test-owner-birth"
    )

    def daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        if method == "agent.create":
            return {"ok": True}
        if method == "agent.runtime-context":
            # Match the real daemon's explicit unconfigured-agent response;
            # the catch-all below is the ps fixture, not a context contract.
            return {"ok": True, "mode": "legacy", "environment": {}}
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": NODE, "owner": OWNER_A}
        }
        response["interactiveSessions"] = [
            {
                "actor": CANONICAL_A,
                "sessionRef": "11111111-2222-3333-4444-555555555555",
                "channelConfirmed": True,
            }
        ]
        return response

    observed: dict[str, Any] = {}

    class Process:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self.returncode: int | None = None
            observed["argv"] = list(argv)
            config_path = Path(argv[argv.index("--mcp-config") + 1])
            observed["config_bytes"] = config_path.read_bytes()

        def poll(self) -> int | None:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkey.setenv("HYPRIAL_HOME", str(hyprial_home))
    monkey.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkey.setenv("HARNESS_CLAUDE_BIN", "/test/bin/claude")
    monkey.setattr(_cli_support, "_daemon_request", daemon_request)
    monkey.setattr(_cli_subprocess, "Popen", Process)
    try:
        result = CliRunner().invoke(
            _cli_root_app,
            ["agent", "start", "claude", "--name", "alice", "--cwd", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
    finally:
        monkey.undo()

    config_text = observed["config_bytes"].decode("utf-8")
    assert "agent:" not in config_text, config_text
    config = json.loads(config_text)
    (server_args,) = [value["args"] for value in config["mcpServers"].values()]
    actor_flag = server_args.index("--actor")
    assert server_args[actor_flag + 1] == "alice"
    assert "agent:" not in " ".join(observed["argv"])


# --------------------------------------------------------------------------- #
# F2: an owner-segment-only relocation is the same session
# --------------------------------------------------------------------------- #
def test_owner_migration_relocated_session_refreshes_and_reads_as_same_session(
    tmp_path: Path,
) -> None:
    """A carrier spelling the pre-migration URI keeps working after migration.

    Real owner migration (daemon generation 2 runs migrate_owner_if_needed in
    its constructor) rewrites the stored session address and the inbox keys to
    the new owner. RED before F2: refresh came back SESSION_SUPERSEDED -- the
    terminal verdict that makes the still-running carrier go quiet, which is
    how both production sessions lost inbound delivery in #352.
    """

    inbox, transport = _inbox_service(tmp_path)
    gen1 = DaemonRig(tmp_path, owner=OWNER_A)
    gen1.attach_inbox(inbox)
    registered = gen1.register(ACTOR, SESSION_REF)
    assert registered["ok"] is True
    _delivered(inbox, transport, "m1", CANONICAL_A)
    gen1.close()

    gen2 = DaemonRig(tmp_path, owner=OWNER_B)
    gen2.attach_inbox(inbox)
    try:
        # The migration really moved the stored addresses.
        sessions = gen2.app.desired_state.load().interactive_sessions
        assert [(s.actor, s.session_ref) for s in sessions] == [
            (CANONICAL_B, SESSION_REF)
        ]
        assert [m.message_id for m in inbox.pending_messages(CANONICAL_B)] == ["m1"]

        # The SAME carrier still announces the pre-migration four-segment URI
        # (a config file written before F1, still running).
        refreshed = gen2.app.handle(
            "session.refresh",
            {"actor": CANONICAL_A, "sessionRef": SESSION_REF},
        )
        assert refreshed["refreshed"] is True
        assert refreshed["actor"] == CANONICAL_B

        assert (
            gen2.app.handle(
                "session.heartbeat",
                {"actor": CANONICAL_A, "sessionRef": SESSION_REF},
            )["alive"]
            is True
        )

        fetched = gen2.fetch(CANONICAL_A, SESSION_REF)
        assert [item["messageId"] for item in fetched["messages"]] == ["m1"]
        assert inbox.has_fetched("m1") is True

        sent = gen2.app.handle(
            "message.send",
            {
                "from": CANONICAL_A,
                "sessionRef": SESSION_REF,
                "to": [CANONICAL_B],
                "summary": "post-migration sender",
                "message": "post-migration sender",
            },
        )
        assert sent["actingPrincipal"] == CANONICAL_B
    finally:
        gen2.close()
        inbox.close()


def test_rename_to_a_different_actor_name_is_still_a_supersede(
    tmp_path: Path,
) -> None:
    """F2's line: only the OWNER segment may differ; a different actor NAME
    holding the same session_ref is a genuine rename/supersede."""

    gen = DaemonRig(tmp_path, owner=OWNER_A)
    inbox, transport = _inbox_service(tmp_path)
    gen.attach_inbox(inbox)
    try:
        gen.register(ACTOR, SESSION_REF)
        # A different session registered the same ref under another NAME --
        # last-writer-wins retires the old registration (existing semantics,
        # pinned by test_mcp_channel_supersede.py at the daemon level).
        gen.register("alicia", SESSION_REF)
        with pytest.raises(DaemonRequestError) as rejected:
            gen.app.handle(
                "session.refresh",
                {"actor": ACTOR, "sessionRef": SESSION_REF},
            )
        assert rejected.value.code == "SESSION_SUPERSEDED"
    finally:
        gen.close()
        inbox.close()


# --------------------------------------------------------------------------- #
# F3: a rejected (or unfenced) fetch commits nothing
# --------------------------------------------------------------------------- #
def test_superseded_fetch_commits_nothing_and_the_holder_takes_the_message(
    tmp_path: Path,
) -> None:
    """The spec's F3 scenario: old session's fetch is rejected -> no fetched
    record, no outbox retirement, the row stays pending, and the session that
    holds the actor fetches the SAME message_id afterwards."""

    inbox, transport = _inbox_service(tmp_path)
    gen = DaemonRig(tmp_path, owner=OWNER_A)
    gen.attach_inbox(inbox)
    try:
        gen.register(ACTOR, "session-old")
        gen.register(ACTOR, "session-new")  # supersedes session-old
        _delivered(inbox, transport, "m1", CANONICAL_A)
        assert inbox.outbox_count() == 1

        with pytest.raises(DaemonRequestError) as rejected:
            gen.fetch(ACTOR, "session-old")
        assert rejected.value.code == "SESSION_SUPERSEDED"

        # Nothing was committed by the rejected fetch.
        assert inbox.has_fetched("m1") is False
        assert [m.message_id for m in inbox.pending_messages(CANONICAL_A)] == ["m1"]
        assert inbox.outbox_count() == 1

        # The holder of the actor retrieves the same row.
        fetched = gen.fetch(ACTOR, "session-new")
        assert [item["messageId"] for item in fetched["messages"]] == ["m1"]
        assert inbox.has_fetched("m1") is True
        assert inbox.outbox_count() == 0
    finally:
        gen.close()
        inbox.close()


def test_unfenced_explicit_fetch_is_rejected_without_committing(
    tmp_path: Path,
) -> None:
    """``fetched=true`` without the session fence must not commit a fetch.

    The explicit-fetch marker belongs to the session-carrier contract; a caller
    that signs no sessionRef owns no inbox custody, so committing its fetch
    would consume another session's delivery from outside any ownership check.
    RED before F3: the daemon happily stamped ``fetched`` and retired the
    sender's outbox for an unfenced caller.
    """

    inbox, transport = _inbox_service(tmp_path)
    gen = DaemonRig(tmp_path, owner=OWNER_A)
    gen.attach_inbox(inbox)
    try:
        gen.register(ACTOR, SESSION_REF)
        _delivered(inbox, transport, "m1", CANONICAL_A)

        with pytest.raises(DaemonRequestError) as rejected:
            gen.app.handle(
                "message.pending.list",
                {"actor": CANONICAL_A, "fetched": True},
            )
        assert rejected.value.code == "STALE_SESSION"

        assert inbox.has_fetched("m1") is False
        assert inbox.outbox_count() == 1
        assert [m.message_id for m in inbox.pending_messages(CANONICAL_A)] == ["m1"]

        # The fenced carrier still takes the same row afterwards.
        fetched = gen.fetch(ACTOR, SESSION_REF)
        assert [item["messageId"] for item in fetched["messages"]] == ["m1"]
    finally:
        gen.close()
        inbox.close()
