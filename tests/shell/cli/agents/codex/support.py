"""PR1 public seams for the Codex app-server/TUI interactive launcher."""

from __future__ import annotations


import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.harnesses import codex
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

THREAD_ID = "thread-11111111-2222-3333-4444-555555555555"
ACTOR = "agent:op:node-1:codex-tui"


def _prompt(message: dict[str, object], sender: str | None = None) -> str:
    """The fake daemon's rendered batch text for one pending row.

    The real daemon renders ``inbox-batch-v1``; a fake only has to be stable so
    a prompt comparison proves the carrier injected daemon text unchanged.
    """
    return _batch_prompt([message])


def _batch_prompt(rows: list[dict[str, object]]) -> str:
    return (
        "[Fake inbox batch cursor=fake-codex]\n"
        + "\n".join(str(row["messageId"]) for row in rows)
    )


def _batch_answer(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "ok": True,
        "schemaVersion": 1,
        "format": "inbox-batch-v1",
        "cursor": "fake-codex",
        "daemonEpoch": "e1",
        "actor": ACTOR,
        "itemCount": len(rows),
        "totalBodyBytes": 0,
        "inlineLimitBytes": 16384,
        "items": rows,
        "delivery": {
            "mode": "inline",
            "text": _batch_prompt(rows),
            "spillPath": None,
            "spillBytes": None,
            "spillSha256": None,
        },
    }


def batch_route(responder: Any) -> Any:
    """Wrap a fake ``daemon_request`` so the batch read is answered too.

    The batch answer is derived from the same pending-list truth the fake
    already keeps, so a carrier under test crosses one consuming read exactly
    as it would against a real daemon.
    """

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "message.pending.batch":
            listed = responder("message.pending.list", dict(params))
            rows = listed.get("messages") if isinstance(listed, dict) else None
            return _batch_answer(list(rows) if isinstance(rows, list) else [])
        return responder(method, params)

    return request


def _hint(method: str, params: dict[str, object]) -> dict[str, object] | None:
    """Shared answers: the daemon's settle hint is always ``ack`` here."""

    if method == "message.settle_hint":
        return {"ok": True, "hint": "ack", "pending": True}
    if method in {"session.turn.started", "session.turn.ended"}:
        return {"ok": True}
    return None


@pytest.fixture(autouse=True)
def _legacy_runtime_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """These pre-P22 cases pin the unconfigured interactive mode."""

    monkeypatch.setattr(_cli_get_services(), "_runtime_context_environment", lambda **_kwargs: None)


class RecordingLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.entries.append(("info", event, fields))

    def warn(self, event: str, **fields: object) -> None:
        self.entries.append(("warn", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.entries.append(("error", event, fields))


def _delay_actor_turn_projection(
    carrier: codex.CodexInteractiveCarrier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open the completion-vs-projection race seen on Linux CI.

    ``on_turn_started`` durably records the native turn before returning to
    the I/O worker, then the actor records the same fact while publishing its
    projection.  Holding only that second call leaves the durable mapping
    intact while making the projection lag long enough for the completed turn
    result to be drained.  A carrier must not enqueue the FETCHED projection a
    second time during that window.
    """

    store = carrier._store
    assert store is not None
    original = store.record_turn_started
    calls = 0
    lock = threading.Lock()

    def delayed(actor: str, message_id: str, turn_id: str) -> None:
        nonlocal calls
        original(actor, message_id, turn_id)
        with lock:
            calls += 1
            call = calls
        if call == 2:
            time.sleep(0.2)

    monkeypatch.setattr(store, "record_turn_started", delayed)


def candidate(cwd: Path) -> dict[str, object]:
    return {
        "id": THREAD_ID,
        "cwd": str(cwd.resolve()),
        "canAcceptDirectInput": True,
        "source": "cli",
    }
