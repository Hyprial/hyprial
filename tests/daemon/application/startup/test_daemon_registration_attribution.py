from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application import _HarnessActorRegistration
from hyprial.kernel.impl.log import Logger


class _Handle:
    def __init__(self, *, close_failures: int = 0) -> None:
        self._closed = False
        self.close_failures = close_failures
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("injected handle close failure")
        self._closed = True


def test_explicit_registration_close_records_stable_attribution(
    tmp_path: Path,
) -> None:
    logger = Logger.daemon(tmp_path, name="node-registration-test")
    token = _Handle()
    endpoint = _Handle()
    registration = _HarnessActorRegistration(
        token,
        endpoint,
        actor_uri="agent:operator:node:squire",
        layer="aggregate",
        event_sink=lambda level, event, **fields: logger.bind(
            component="daemon"
        ).log(level, event, **fields),
    )

    registration.close(reason="actor-inactive", initiator="daemon-runtime")
    registration.close(reason="daemon-stop", initiator="daemon-runtime")

    [entry] = [
        json.loads(line)
        for line in (tmp_path / "logs" / "daemon.jsonl").read_text().splitlines()
    ]
    assert entry == {
        "ts": entry["ts"],
        "level": "info",
        "component": "daemon",
        "name": "node-registration-test",
        "event": "harness.actor.registration.closed",
        "actorId": "agent:operator:node:squire",
        "registrationLayer": "aggregate",
        "reason": "actor-inactive",
        "initiator": "daemon-runtime",
        "origin": "explicit-close",
    }
    assert token._closed is True
    assert endpoint._closed is True


def test_child_handle_invalidation_is_attributed_once_before_rebuild() -> None:
    events: list[dict[str, Any]] = []
    token = _Handle()
    endpoint = _Handle()
    registration = _HarnessActorRegistration(
        token,
        endpoint,
        actor_uri="agent:operator:node:squire",
        layer="aggregate",
        event_sink=lambda level, event, **fields: events.append(
            {"level": level, "event": event, **fields}
        ),
    )

    endpoint.close()

    assert registration.healthy is False
    assert registration.healthy is False
    assert events == [
        {
            "level": "warn",
            "event": "harness.actor.registration.invalidated",
            "actorId": "agent:operator:node:squire",
            "registrationLayer": "aggregate",
            "reason": "child-handle-closed",
            "initiator": "health-probe",
            "origin": "externally-observed",
            "unhealthyLayers": ["aggregate.endpoint"],
        }
    ]

    registration.close(
        reason="reconcile-unhealthy",
        initiator="daemon-runtime",
    )
    assert events[-1]["event"] == "harness.actor.registration.closed"
    assert events[-1]["reason"] == "reconcile-unhealthy"


def test_unspecified_external_close_records_sanitized_caller() -> None:
    events: list[dict[str, Any]] = []
    registration = _HarnessActorRegistration(
        _Handle(),
        _Handle(),
        actor_uri="agent:operator:node:squire",
        layer="aggregate",
        event_sink=lambda level, event, **fields: events.append(
            {"level": level, "event": event, **fields}
        ),
    )

    registration.close()

    assert events[0]["reason"] == "unspecified"
    assert events[0]["initiator"] == "external-caller"
    assert events[0]["caller"].startswith(
        'test_daemon_registration_attribution.py:test_unspecified_external_close_records_sanitized_caller:'
    )
    assert "/" not in events[0]["caller"]


def test_logging_failure_does_not_block_registration_close() -> None:
    token = _Handle()
    endpoint = _Handle()

    def unavailable_log(_level: str, _event: str, **_fields: Any) -> None:
        raise OSError("log sink unavailable")

    registration = _HarnessActorRegistration(
        token,
        endpoint,
        actor_uri="agent:operator:node:squire",
        layer="aggregate",
        event_sink=unavailable_log,
    )

    registration.close(reason="daemon-stop", initiator="daemon-runtime")

    assert token._closed is True
    assert endpoint._closed is True


def test_partial_child_close_retries_only_unfinished_handle() -> None:
    events: list[dict[str, Any]] = []
    token = _Handle()
    endpoint = _Handle(close_failures=1)
    registration = _HarnessActorRegistration(
        token,
        endpoint,
        actor_uri="agent:operator:node:worker",
        layer="aggregate",
        event_sink=lambda level, event, **fields: events.append(
            {"level": level, "event": event, **fields}
        ),
    )

    try:
        registration.close(reason="prepare-failed", initiator="route-owner")
    except RuntimeError as error:
        assert "partial harness actor registration cleanup" in str(error)
    else:
        raise AssertionError("first partial close must report its retained failure")
    assert endpoint.close_calls == 1
    assert token.close_calls == 1
    assert token._closed is True
    assert endpoint._closed is False

    registration.close(reason="prepare-retry", initiator="route-owner")
    registration.close(reason="redundant", initiator="route-owner")
    assert endpoint.close_calls == 2
    assert token.close_calls == 1
    assert endpoint._closed is True
    assert [event["event"] for event in events] == [
        "harness.actor.registration.closed"
    ]
