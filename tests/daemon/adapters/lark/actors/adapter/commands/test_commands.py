from __future__ import annotations

import json
from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark import AgentDirectoryEntry, ActorTarget, RuntimeTarget
from hyprial.daemon.impl.adapters.lark.worker.daemon_link import _Routes
from hyprial.daemon.impl.adapters.lark.worker import daemon_link as lark_worker_daemon_link

from ..support import (
    actor as actor,
    make_adapter,
    runtime_target,
    sdk_event,
)


def test_agents_lists_agent_entities_with_current_status_and_metadata(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.agent_entries = (
        AgentDirectoryEntry(
            name="alice",
            status="running",
            pinned_adapters=("lark-main",),
            preferred_harness="claude",
        ),
        AgentDirectoryEntry(
            name="offline",
            status="down",
            preferred_harness="pi",
        ),
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_agents",
        message_id="om_agents",
        text="/hyprial agents",
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    assert "Agent entities" in lark.last_reply_text
    assert "alice | running | lark-main | claude" in lark.last_reply_text
    assert "offline | down | — | pi" in lark.last_reply_text


def test_agents_empty_directory_is_friendly(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_agents_empty",
        message_id="om_agents_empty",
        text="/hyprial agents",
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    assert "No agent entities are registered or present." in lark.last_reply_text


def test_worker_agent_directory_joins_registry_records_with_live_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_ipc(_socket: Path, method: str, _params: dict[str, object]):
        if method == "ps":
            return {
                "agents": [
                    {
                        "uri": "agent:alice:mac:worker",
                        "actor": "worker",
                        "status": "offline",
                        "pinnedAdapters": ["lark-main"],
                        "preferredHarness": "claude",
                    }
                ]
            }
        if method == "targets":
            return {
                "targets": [
                    {
                        "targetKind": "agent",
                        "targetUri": "agent:alice:mac:worker",
                        "actor": "agent:alice:mac:worker",
                        "status": "online",
                    },
                    {
                        "targetKind": "agent",
                        "targetUri": "agent:bob:linux:pi",
                        "actor": "agent:bob:linux:pi",
                        "status": "online",
                    },
                ]
            }
        raise AssertionError(method)

    monkeypatch.setattr(lark_worker_daemon_link, "_ipc", fake_ipc)
    routes = _Routes(
        gateway_name="lark-main", socket_path=tmp_path / "daemon.sock"
    )

    assert routes.list_agent_directory() == (
        AgentDirectoryEntry(
            name="worker",
            status="running",
            pinned_adapters=("lark-main",),
            preferred_harness="claude",
        ),
        AgentDirectoryEntry(name="agent:bob:linux:pi", status="running"),
    )


@pytest.mark.parametrize(
    ("contents", "expected"),
    (
        (
            "meta:\n  version: 7\n  issued_at: 2026-08-16\n  publisher: alicesmith\n"
            "lines:\n  - hyprial\n# accepted context\nowner: hq",
            "version=7 issued_at=2026-08-16 publisher=alicesmith",
        ),
        (
            None,
            "org-context absent — 尚未采信任何组织上下文",
        ),
        (
            "meta:\n  version: 7\n  issued_at:\n  publisher: alicesmith\n# bad",
            "org-context absent — 尚未采信任何组织上下文",
        ),
    ),
)
def test_org_summary_reads_meta_and_degrades_to_absent(
    tmp_path: Path,
    actor: ActorTarget,
    contents: str | None,
    expected: str,
) -> None:
    org_path = tmp_path / "org-context.md"
    if contents is not None:
        org_path.write_text(contents, encoding="utf-8")
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id=f"evt_org_{expected[:4]}",
        message_id=f"om_org_{expected[:4]}",
        text="/hyprial org",
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    assert expected in lark.last_reply_text
    if expected.startswith("version="):
        assert "preview:" in lark.last_reply_text
        assert "# accepted context" in lark.last_reply_text


def test_ask_exact_runtime_target_forwards_only_message_body(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, routes, _events = make_adapter(tmp_path, actor)
    target = runtime_target()
    routes.runtime_targets = (target,)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_ask",
        message_id="om_ask",
        text=f"/hyprial ask {target.target_uri} inspect the queue",
    )

    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.to.actor_id == target.target_uri
    assert request.text == "inspect the queue"
    assert "/hyprial" not in request.text


def test_ask_preserves_the_complete_multiline_message_body(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, _lark, harness, routes, _events = make_adapter(tmp_path, actor)
    target = runtime_target()
    routes.runtime_targets = (target,)
    body = "inspect the queue\nthen report verbatim"

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_ask_multiline",
        message_id="om_ask_multiline",
        text=f"/hyprial ask {target.target_uri} {body}",
    )

    assert len(harness.requests) == 1
    assert harness.requests[0].text == body


@pytest.mark.parametrize(
    ("targets", "token", "expected"),
    (
        ((), "ghost", "TARGET_NOT_FOUND"),
        (
            (
                runtime_target("agent:alice:mac:worker"),
                runtime_target("agent:bob:linux:worker"),
            ),
            "worker",
            "AMBIGUOUS_TARGET",
        ),
    ),
)
def test_ask_unknown_or_ambiguous_target_fails_locally_without_forwarding(
    tmp_path: Path,
    actor: ActorTarget,
    targets: tuple[RuntimeTarget, ...],
    token: str,
    expected: str,
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.runtime_targets = targets

    sdk_event(
        adapter.handle_sdk_event,
        event_id=f"evt_{expected}",
        message_id=f"om_{expected}",
        text=f"/hyprial ask {token} do it",
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    assert expected in lark.last_reply_text
    if expected == "AMBIGUOUS_TARGET":
        assert "agent:alice:mac:worker" in lark.last_reply_text
        assert "agent:bob:linux:worker" in lark.last_reply_text


def test_agents_and_ambiguity_replies_are_deterministic_and_byte_bounded(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    # Quotes and backslashes expand on the actual SDK wire even though the raw
    # reply text stays the same size.  Multibyte text exercises the other side
    # of the UTF-8 accounting boundary.
    suffix = ('\\"' * 300) + ("界" * 300)
    routes.runtime_targets = tuple(
        runtime_target(f"agent:node-{index:04d}:{suffix}:shared")
        for index in reversed(range(500))
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_many_agents",
        message_id="om_many_agents",
        text="/hyprial agents",
    )
    listing = lark.last_reply_text or ""
    assert (
        len(json.dumps({"text": listing}, ensure_ascii=False).encode("utf-8"))
        < 150_000
    )
    assert "shown=" in listing and "total=500" in listing
    assert listing.find("node-0000") < listing.find("node-0001")

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_many_aliases",
        message_id="om_many_aliases",
        text="/hyprial ask shared do it",
    )
    ambiguity = lark.last_reply_text or ""
    assert harness.requests == []
    assert (
        len(json.dumps({"text": ambiguity}, ensure_ascii=False).encode("utf-8"))
        < 150_000
    )
    assert "shown=" in ambiguity and "total=500" in ambiguity


def test_ask_rejects_an_unsafe_runtime_target_returned_by_directory(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.runtime_targets = (
        RuntimeTarget(
            target_uri="agent:forged\nheader",
            actor="forged\nheader",
            alias="safe-alias",
            status="online",
        ),
    )

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_unsafe_directory_target",
        message_id="om_unsafe_directory_target",
        text="/hyprial ask safe-alias do it",
    )

    assert harness.requests == []
    assert "INVALID_TARGET_RESPONSE" in (lark.last_reply_text or "")


def test_ask_rejects_a_control_character_target_before_directory_lookup(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_control_target",
        message_id="om_control_target",
        text="/hyprial ask bad\x00target do it",
    )

    assert harness.requests == []
    assert "INVALID_TARGET" in (lark.last_reply_text or "")


@pytest.mark.parametrize("text", ("/hyprial agents pin worker", "/hyprial agents unpin"))
def test_pin_commands_show_the_current_pin_and_name_the_exact_cli_remedy(
    tmp_path: Path, actor: ActorTarget, text: str
) -> None:
    """Read-only pin visibility in chat, plus the exact operator command.

    Writing a pin from chat stays refused (no operator identity in Lark),
    but the reply must never be a dead end: it shows which agent this
    adapter is pinned to and the exact CLI including the adapter's name.
    """

    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id=f"evt_{text}",
        message_id=f"om_{text}",
        text=text,
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    # make_adapter pins the DM to the fixture actor; that binding is shown.
    assert f"pinned to: {actor.actor_id}" in lark.last_reply_text
    assert "not supported in Lark chat" in lark.last_reply_text
    # The command names THIS adapter ("bot" from channel:lark:tenant:bot).
    assert "hyprial adapter pin bot <agent>" in lark.last_reply_text
    assert "hyprial adapter unpin bot" in lark.last_reply_text


@pytest.mark.parametrize("text", ("/hyprial agents pin worker", "/hyprial agents unpin"))
def test_pin_commands_without_a_pin_say_so(
    tmp_path: Path, actor: ActorTarget, text: str
) -> None:
    adapter, lark, harness, routes, _events = make_adapter(tmp_path, actor)
    routes.pins.clear()

    sdk_event(
        adapter.handle_sdk_event,
        event_id=f"evt_{text}",
        message_id=f"om_{text}",
        text=text,
    )

    assert harness.requests == []
    assert lark.last_reply_text is not None
    assert "no pinned agent" in lark.last_reply_text
    assert "hyprial adapter pin bot <agent>" in lark.last_reply_text


def test_route_command_is_consumed_with_explicit_unsupported_response(
    tmp_path: Path, actor: ActorTarget
) -> None:
    adapter, lark, harness, _routes, _events = make_adapter(tmp_path, actor)

    sdk_event(
        adapter.handle_sdk_event,
        event_id="evt_route",
        message_id="om_route",
        text="/hyprial route list",
    )

    assert harness.requests == []
    assert "not supported" in (lark.last_reply_text or "")
