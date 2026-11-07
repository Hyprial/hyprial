"""Cross-component actor identity contract: pins, registrations, sends.

Incident: a Lark channel pin stored the canonical URI
``agent:<owner>:<machine>:hq-adjutant`` while ``hyprial agent start claude --name
hq-adjutant`` registered the interactive session under the bare name.  The
daemon declared the session's liveliness token and inbox endpoint under the
bare key, so sends addressed to the pinned canonical URI matched nothing and
were durable-queued behind ``ok:true`` with only an info-level
``send.received`` — minutes of silently lost messages.

These tests pin the contract across the three places identity is expressed
(pins, session registration, send addressing): any spelling combination must
either deliver to the one registered identity or fail loudly.  A silent
queue-forever under a key nobody drains is forbidden.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path
import time
from typing import Any

from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import InteractiveSession
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

OWNER = "op"
NODE = "node-1"
ACTOR = "hq-adjutant"
CANONICAL = f"agent:{OWNER}:{NODE}:{ACTOR}"


def seed_legacy_row(inbox: InboxService, message: InboxMessage) -> None:
    """Insert an inbox row directly, as if it predated the ingress gate.

    ``receive()`` now rejects bare-sender messages (Allen B); rows like this
    one still exist in upgraded databases, so tests verifying their
    continued consumption seed through SQL, not through the gated path.
    The terminal record is written too: a real pre-gate receive committed
    both in one transaction.
    """

    from hyprial.daemon.impl.inbox import HoldReason, TerminalState

    with inbox._db:  # type: ignore[attr-defined]
        inbox._db.execute(  # type: ignore[attr-defined]
            """INSERT INTO inbox (
                   message_id, conversation_id, sender, recipient, payload, intent,
                   lifecycle, idempotency_key, created_at_ms, received_at_ms,
                   origin_node
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.message_id,
                message.conversation_id,
                message.sender,
                message.recipient,
                message.payload,
                message.intent,
                message.lifecycle.value,
                message.idempotency_key,
                message.created_at_ms,
                0,
                message.origin_node,
            ),
        )
        inbox._record_terminal(  # type: ignore[attr-defined]
            message,
            state=TerminalState.FETCHED,
            reason=HoldReason.ACK_RECEIVED,
            now_ms=0,
        )


class _Presence:
    def __init__(self, actors: tuple[str, ...]) -> None:
        self._actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self._actors

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _WireTransport(MemoryDeliveryTransport):
    """Records delivered frames and auto-confirms direct delivery."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[InboxMessage] = []

    def deliver(self, message: InboxMessage) -> bool:
        self.frames.append(message)
        self.confirm_direct(message.message_id)
        return super().deliver(message)


def application(
    tmp_path: Path,
    *,
    actors: tuple[str, ...] = (),
    providers: tuple[dict[str, object], ...] = (),
) -> DaemonApplication:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": list(providers),
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _Presence(actors)
    return app


def register_interactive(
    app: DaemonApplication, actor: str, session_ref: str = "session-1"
) -> str:
    """Register through the real daemon method; return the stored actor key."""

    result = app.handle(
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
    assert result["registered"] is True
    (stored,) = [
        session.actor
        for session in app.desired_state.load().interactive_sessions
        if session.session_ref == session_ref
    ]
    return stored


def wire_inbox(app: DaemonApplication, tmp_path: Path, name: str) -> _WireTransport:
    transport = _WireTransport()
    app._inbox = InboxService(tmp_path / name, transport)
    return transport


def send(app: DaemonApplication, target: str) -> dict[str, Any]:
    result = app.handle(
        "message.send",
        {"from": "channel:lark:main", "to": [target], "message": "ping", "summary": "ping"},
    )
    (delivery,) = result["deliveries"]
    return delivery


def log_entries(app: DaemonApplication) -> list[dict[str, Any]]:
    path = app.state_dir / "logs" / "daemon.jsonl"
    status = app._logger.writer_status()
    assert status is not None
    assert app._logger.wait_for_writes(
        target=status.accepted, rejected_at_capture=status.rejected, timeout=2.0
    ), (
        app._logger.writer_status()
    )
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_daemon_normalizes_bare_session_registration_to_canonical(
    tmp_path: Path,
) -> None:
    """The daemon boundary closes the raw-MCP bypass around CLI normalization."""

    app = application(tmp_path)
    registered_as = register_interactive(app, ACTOR)
    assert registered_as == CANONICAL
    assert [agent.uri for agent in app.agents.list()] == [CANONICAL]


def test_old_mcp_child_can_keep_using_bare_actor_after_canonical_registration(
    tmp_path: Path,
) -> None:
    """Old clients keep their wire spelling; daemon state and inbox keys do not."""

    app = application(tmp_path)
    registered_as = register_interactive(app, ACTOR)
    transport = wire_inbox(app, tmp_path, "inbox-old-mcp.sqlite3")
    transport.set_online(registered_as, True)

    pending = app.handle(
        "message.pending.list",
        {"actor": ACTOR, "sessionRef": "session-1"},
    )
    delivery = send(app, ACTOR)

    assert pending["ok"] is True
    assert delivery["accepted"] is True
    assert delivery["queued"] is False
    (frame,) = transport.frames
    assert frame.recipient == CANONICAL


def test_unfenced_cli_ack_keeps_the_node_mailbox_spelling(tmp_path: Path) -> None:
    """The operator settles a legacy node-mailbox row; an unfenced claim cannot.

    contract/wire/message-sender-auth.md §System-notice acknowledgement: only
    an authenticated local operator may use the node id as an additional
    notice key, and an unfenced node-name claim must not acknowledge the
    operator's notices.  The legacy rows keep the bare node spelling, so the
    operator path must still reach them.
    """

    app = application(tmp_path)
    assert app._inbox is not None
    message = InboxMessage(
        message_id="node-message",
        conversation_id="node-conversation",
        sender="sender-node",
        recipient=NODE,
        payload=b'{"message":"before upgrade"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )
    seed_legacy_row(app._inbox, message)

    claimed = app.handle(
        "message.ack", {"from": NODE, "messageId": message.message_id}
    )
    assert claimed["acknowledged"] is False
    assert claimed["code"] == "MESSAGE_ACK_UNAVAILABLE"
    assert app._inbox.pending_count(NODE) == 1

    result = app.handle(
        "message.ack", {"from": f"user:{OWNER}", "messageId": message.message_id}
    )

    assert result["acknowledged"] is True
    assert app._inbox.pending_count(NODE) == 0


def test_bare_target_delivers_to_canonical_registered_session(
    tmp_path: Path,
) -> None:
    """The reverse spelling: session registered canonical, sender typed the
    bare actor name."""

    app = application(tmp_path, actors=(CANONICAL,))
    registered_as = register_interactive(app, CANONICAL)
    assert registered_as == CANONICAL
    transport = wire_inbox(app, tmp_path, "inbox-canonical.sqlite3")
    transport.set_online(registered_as, True)

    delivery = send(app, ACTOR)

    assert delivery["accepted"] is True
    assert delivery["queued"] is False
    (frame,) = transport.frames
    assert frame.recipient == CANONICAL


def test_bare_target_queues_under_the_registered_key_of_offline_session(
    tmp_path: Path,
) -> None:
    """An offline canonical-registered session must still receive bare-name
    sends: queue under the key the session drains on restart, never under a
    bare alias nobody consumes — and stay quiet, because the identity is
    known and the durable queue is the designed behavior."""

    app = application(tmp_path)
    register_interactive(app, CANONICAL)

    delivery = send(app, ACTOR)

    assert delivery["target"] == CANONICAL
    assert delivery["queued"] is True
    assert "code" not in delivery
    assert not [
        entry for entry in log_entries(app) if entry["level"] == "warn"
    ]


def test_unknown_local_canonical_uri_fails_loud(tmp_path: Path) -> None:
    """A canonical URI in THIS node's owner/machine namespace that no
    registration, provider, worker, or online actor claims can never drain:
    the node is the authority for its own identities.  The delivery must
    carry an observable code and the daemon must log a warn — never the
    silent queue-forever of the incident."""

    app = application(tmp_path)

    delivery = send(app, f"agent:{OWNER}:{NODE}:ghost")

    assert delivery["queued"] is True
    assert delivery["code"] == "TARGET_UNRESOLVED"
    (warn,) = [
        entry
        for entry in log_entries(app)
        if entry["level"] == "warn" and entry["event"] == "send.target_unresolved"
    ]
    assert warn["target"] == f"agent:{OWNER}:{NODE}:ghost"
    assert warn["messageId"] == delivery["messageId"]
    assert warn["correlationId"] == delivery["messageId"]
    assert warn["node"] == "daemon-send"
    assert warn["component"] == "daemon"
    assert warn["conversationId"]
    assert warn["sender"] == "channel:lark:main"
    assert isinstance(warn["ts"], str)


def test_unknown_local_rejection_never_refreshes_or_waits_for_harness(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)

    class BlockingHarness:
        def session_refs(self) -> object:
            raise AssertionError("unknown-local classification must not refresh Harness")

    app._harnesses = BlockingHarness()  # type: ignore[assignment]
    began = time.monotonic()
    delivery = send(app, f"agent:{OWNER}:{NODE}:ghost")
    assert time.monotonic() - began < 0.2
    assert delivery["code"] == "TARGET_UNRESOLVED"


def test_remote_canonical_uri_keeps_quiet_durable_queue(tmp_path: Path) -> None:
    """A URI in another node's namespace may legitimately come online later;
    this node is not its authority, so durable queueing stays quiet."""

    app = application(tmp_path)

    delivery = send(app, "agent:mate:peer-host:ghost")

    assert delivery["queued"] is True
    assert "code" not in delivery
    assert not [
        entry
        for entry in log_entries(app)
        if entry["event"] == "send.target_unresolved"
    ]


def test_known_offline_provider_uri_stays_quiet(tmp_path: Path) -> None:
    """A managed provider's minted URI is a known identity even while the
    worker is down: it queues quietly for the connector to drain."""

    provider = {
        "provider": "pi",
        "name": "kanban-sim",
        "headless": True,
        "args": [],
        "ownership": "managed",
    }
    app = application(tmp_path, providers=(provider,))

    delivery = send(app, f"agent:{OWNER}:{NODE}:kanban-sim")

    assert delivery["queued"] is True
    assert "code" not in delivery
    assert not [
        entry
        for entry in log_entries(app)
        if entry["event"] == "send.target_unresolved"
    ]


def test_bare_and_canonical_registration_supersede_on_one_canonical_key(
    tmp_path: Path,
) -> None:
    """Registration spelling can no longer create two network identities."""

    app = application(tmp_path)
    register_interactive(app, ACTOR, session_ref="session-legacy")
    register_interactive(app, CANONICAL, session_ref="session-canonical")

    sessions = app.desired_state.load().interactive_sessions
    assert [(item.actor, item.session_ref) for item in sessions] == [
        (CANONICAL, "session-canonical")
    ]


def test_restart_migrates_legacy_bare_session_before_route_restore(
    tmp_path: Path,
) -> None:
    app = application(tmp_path)
    app.desired_state.register_interactive(
        InteractiveSession(
            actor=ACTOR,
            cwd="/work",
            command=("claude",),
            source="claude-channel",
            session_ref="legacy-session",
        )
    )

    app._normalize_persisted_interactive_sessions()

    (session,) = app.desired_state.load().interactive_sessions
    assert session.actor == CANONICAL
    assert session.session_ref == "legacy-session"


# --- CLI registration-side normalization -------------------------------
#
# ``hyprial agent start claude --name <name>`` is where the incident's bare
# registration entered the system: the spawned MCP channel child registered
# whatever string the operator typed.  The CLI must mint the same canonical
# ``agent:<owner>:<machine>:<actor>`` form the pins and providers use, while
# Claude's own display name stays short.


class _Process:
    """Immediately-registered fake Claude process."""

    def __init__(self, argv: list[str], observed: dict[str, Any]) -> None:
        self.returncode: int | None = None
        self._observed = observed
        observed["argv"] = argv
        try:
            observed["session_ref"] = argv[argv.index("--session-id") + 1]
        except ValueError as error:
            raise AssertionError(
                f"missing session identity in argv: {argv!r}"
            ) from error
        config_path = Path(argv[argv.index("--mcp-config") + 1])
        observed["config"] = json.loads(config_path.read_text(encoding="utf-8"))

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is None
        self.returncode = 0
        return 0


def _run_interactive_start(
    monkeypatch: Any,
    tmp_path: Path,
    name: str,
) -> tuple[Any, dict[str, Any]]:
    from hyprial.daemon.impl.mcp import channel as mcp_channel

    observed: dict[str, Any] = {}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        if method == "agent.runtime-context":
            # The real daemon returns this exact shape for an unconfigured
            # identity (DaemonApplication.handle); this fixture must not let
            # its generic ps response masquerade as a runtime-context reply.
            return {"ok": True, "mode": "legacy", "environment": {}}
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": NODE, "owner": OWNER}
        }
        if "session_ref" in observed:
            response["interactiveSessions"] = [
                {
                    "actor": observed.get("registered_actor", ""),
                    "sessionRef": observed["session_ref"],
                    "channelConfirmed": True,
                }
            ]
        return response

    def popen(argv: list[str], **kwargs: Any) -> _Process:
        process = _Process(argv, observed)
        # The daemon canonicalizes whatever --actor spelling the child sends
        # and stores the four-segment URI — the launcher's registration poll
        # matches against that canonical form.
        observed["registered_actor"] = CANONICAL
        return process

    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/test/bin/claude")
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_subprocess, "Popen", popen)
    # Process-birth probing also uses subprocess on macOS.  It is orthogonal
    # to this launch-identity contract and must not be captured as the Claude
    # process by the Popen fake above.
    monkeypatch.setattr(mcp_channel, "_read_process_identity", lambda _pid: None)

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "claude", "--name", name, "--cwd", str(tmp_path), "--json"],
    )
    return result, observed


def test_start_claude_normalizes_bare_name_to_canonical_actor(
    monkeypatch: Any, tmp_path: Path
) -> None:
    result, observed = _run_interactive_start(monkeypatch, tmp_path, ACTOR)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["actor"] == CANONICAL
    server_args = observed["config"]["mcpServers"]["harness-bridge"]["args"]
    # The launch persists the bare name; the daemon mints the canonical URI
    # per call (owner change must never strand a running channel: #352).
    assert server_args[server_args.index("--actor") + 1] == ACTOR
    assert "agent:" not in json.dumps(observed["config"])
    # Claude's own display name stays the short actor, not the URI.
    argv = observed["argv"]
    assert argv[argv.index("--name") + 1] == ACTOR


def test_start_claude_accepts_already_canonical_name(
    monkeypatch: Any, tmp_path: Path
) -> None:
    result, observed = _run_interactive_start(monkeypatch, tmp_path, CANONICAL)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["actor"] == CANONICAL
    server_args = observed["config"]["mcpServers"]["harness-bridge"]["args"]
    # A four-segment --name is valid input but reduces to its short name on
    # the persisted side: the config and argv never freeze a derived address.
    assert server_args[server_args.index("--actor") + 1] == ACTOR
    argv = observed["argv"]
    assert argv[argv.index("--name") + 1] == ACTOR


def test_start_claude_rejects_malformed_colon_name(
    monkeypatch: Any, tmp_path: Path
) -> None:
    (tmp_path / "home").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {
            "daemon": {"running": True, "nodeId": NODE, "owner": OWNER}
        },
    )

    def popen(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a malformed --name must never spawn a process")

    monkeypatch.setattr(_cli_subprocess, "Popen", popen)

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "claude",
            "--name",
            "agent:two-segments-only",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"
