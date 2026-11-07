"""Consumer-level §7 guards on the unified-hooks path: record per turn, format.

Every assertion here moved from ``tests/daemon/turns/hooks`` with the recap
behaviour unchanged; only the driving path changed (H1 bus + hooks.json v2
``{"consumer": "recap"}`` instead of the retired turn-hooks.json).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hyprial.biz import RecapHookConsumer
from hyprial.daemon.impl.api import HarnessResultStatus, delivery_prompt
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.streaming import (
    ProgressObservation,
    StreamingTurnProcess,
)
from hyprial.kernel import redact_turn_excerpt

from .support import (
    RECIPIENT,
    TURN_HOOK_CLOSE_TIMEOUT_SECONDS,
    build_service,
    observe,
    prime_configuration,
    read_records,
    wait_for_recap_lines,
    write_hooks_config,
)


def test_recap_is_append_only_survives_restart_and_redacts_machine_data(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    write_hooks_config(home)

    bus, first, first_consumer = build_service(home)
    observe(
        first,
        "id-1",
        HarnessResultStatus.COMPLETED,
        output="saved /Users/person/private token=secret-value",
        tool_names=("shell", "/absolute/tool", "shell"),
    )
    path = home / "state" / "recaps.jsonl"
    _wait_one(path)
    first_bytes = path.read_bytes()
    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert first_consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)

    bus2, second, second_consumer = build_service(home)
    observe(second, "id-2", HarnessResultStatus.INTERRUPTED)

    assert path.read_bytes().startswith(first_bytes)
    lines = [json.loads(line) for line in _wait_two(path)]
    assert [line["deliveryId"] for line in lines] == ["id-1", "id-2"]
    assert [line["status"] for line in lines] == ["completed", "interrupted"]
    assert [line["event"] for line in lines] == ["turn", "turn"]
    assert lines[0]["toolNames"] == ["shell"]
    assert "/Users/person/private" not in path.read_text()
    assert "secret-value" not in path.read_text()
    assert bus2.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert second_consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)


def test_one_record_per_turn_for_completed_and_failed_turns(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    write_hooks_config(home)
    bus, service, consumer = build_service(home)

    observe(service, "ok", HarnessResultStatus.COMPLETED, tool_names=("reader",))
    observe(
        service,
        "bad",
        HarnessResultStatus.FAILED,
        failure_code="PROVIDER_TRANSIENT_FAILURE",
    )

    records = read_records(home / "state" / "recaps.jsonl", 2)
    assert [record["status"] for record in records] == ["completed", "failed"]
    assert records[1]["deliveryId"] == "bad"
    assert records[0]["toolNames"] == ["reader"]
    # The neutral record keeps schema 1: existing logs and the
    # recentRecaps reader keep reading it after the H2 move.
    assert all(record["schemaVersion"] == 1 for record in records)
    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)


def test_recap_excerpt_follows_the_versioned_payload_bound(tmp_path: Path) -> None:
    """The record copies the payload's redacted excerpt (≤1000, design §2.2).

    v1 kept the pre-redaction truncation quirk (redaction could expand the
    excerpt past 1000 chars); the v2 event contract bounds it, so the record
    now carries exactly the payload bytes.
    """

    home = tmp_path / "home"
    write_hooks_config(home)
    bus, service, consumer = build_service(home)
    observe(service, "id", HarnessResultStatus.COMPLETED, output=" /a" * 400)

    [record] = read_records(home / "state" / "recaps.jsonl", 1)
    expected = redact_turn_excerpt(" /a" * 400)
    assert record["outputExcerpt"] == expected
    assert len(record["outputExcerpt"]) <= 1000
    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)


def test_default_off_writes_no_recap_and_prompt_is_unchanged(
    tmp_path: Path,
) -> None:
    """No hooks.json: the delivery prompt is byte-identical and no file appears."""

    home = tmp_path / "home"  # deliberately no hooks.json
    delivery = HarnessDelivery(
        "id", "conversation", "agent:owner:node:sender", RECIPIENT, "body"
    )
    expected = (
        "[Harness Network message from agent:owner:node:sender to " + RECIPIENT + "]\nbody"
    )
    bus, service, consumer = build_service(home)

    assert delivery_prompt(delivery).encode() == expected.encode()
    observe(service, "id", HarnessResultStatus.COMPLETED, output="ok")
    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert not (home / "state" / "recaps.jsonl").exists()


def test_no_route_keeps_recap_off_when_other_consumers_are_registered(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    write_hooks_config(home, events=())  # schema v2 present, no entries
    bus, service, consumer = build_service(home)
    prime_configuration(service)

    observe(service, "id", HarnessResultStatus.COMPLETED)

    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert not (home / "state" / "recaps.jsonl").exists()


def test_unresolvable_home_is_logged_and_skipped(tmp_path: Path) -> None:
    logs: list[tuple[str, dict[str, object]]] = []
    consumer = RecapHookConsumer(
        home_for_actor=lambda _actor: None,
        logger=lambda _level, _component, name, **fields: logs.append((name, fields)),
        monotonic=lambda: 0.0,
    )
    event = _synthetic_event("turn.completed")
    consumer.observe(event, {})
    consumer.observe(event, {})  # rate-limited: one line per interval

    assert [name for name, _fields in logs] == ["hooks.recap_home_error"]
    assert consumer.close(1.0)


def test_recent_recaps_serves_the_intercept_reader_and_skips_torn_lines(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    write_hooks_config(home)
    bus, service, consumer = build_service(home)
    observe(service, "one", HarnessResultStatus.COMPLETED)
    observe(service, "two", HarnessResultStatus.COMPLETED)
    path = home / "state" / "recaps.jsonl"
    _wait_two(path)
    with path.open("a", encoding="utf-8") as torn:
        torn.write('{"schemaVersion": 1, "event": "tur')

    served = consumer.recent_recaps(RECIPIENT, 3)

    assert [record["deliveryId"] for record in served] == ["one", "two"]
    assert consumer.recent_recaps(RECIPIENT, 0) == []
    assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)


def _synthetic_event(name: str):  # type: ignore[no-untyped-def]
    from hyprial.kernel import HookEvent, turn_event_payload

    return HookEvent(
        event=name,
        kind="observe",
        emitted_at_ms=1,
        seq=0,
        source="test",
        actor=RECIPIENT,
        payload=turn_event_payload(
            delivery_id="id",
            sender="agent:owner:node:sender",
            conversation="conversation",
            started_at_ms=1,
            ended_at_ms=2,
            status="completed",
            tool_names=(),
            output="",
            prompt="",
            tool_calls=(),
            fidelity="observed",
        ),
    )


class _TurnClient:
    async def __aenter__(self) -> _TurnClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def receive_response(self):  # type: ignore[no-untyped-def]
        yield ProgressObservation("tool-call", "called", tool_name="reader")
        yield SimpleNamespace(result="done", is_error=False)


class _HeldTurnClient(_TurnClient):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self._started = started
        self._release = release

    async def query(self, _prompt: str) -> None:
        self._started.set()
        import asyncio

        await asyncio.to_thread(self._release.wait)

    async def interrupt(self) -> None:
        self._release.set()


def test_completed_observer_writes_the_turn_without_delaying_results(
    tmp_path: Path,
) -> None:
    """A slow recap append never delays drain_results (§7: never delays a turn)."""

    home = tmp_path / "home"
    write_hooks_config(home)
    bus, service, consumer = build_service(home)
    append_entered = threading.Event()
    release_append = threading.Event()
    original_append = consumer._writer._append  # noqa: SLF001

    def observed_append(path, value):  # type: ignore[no-untyped-def]
        append_entered.set()
        assert release_append.wait(1)
        original_append(path, value)

    consumer._writer._append = observed_append  # type: ignore[method-assign]  # noqa: SLF001
    prime_configuration(service)
    process = StreamingTurnProcess(
        harness="test",
        label="test",
        client_factory=_TurnClient,
        thread_name="recap-turn-observer",
        on_turn_completed=service.observe_turn,
    )
    try:
        assert process.wait_ready(timeout=1)
        assert process.enqueue(
            HarnessDelivery("id", "conversation", "sender", RECIPIENT, "body")
        )
        deadline = time.monotonic() + 1
        results = ()
        while not results:
            # Held append proves the observer path never blocks the drain.
            results = process.drain_results()
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert results[0].status is HarnessResultStatus.COMPLETED
        assert append_entered.wait(1)
    finally:
        release_append.set()
        process.stop()
        assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
        assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    record = json.loads(
        wait_for_recap_lines(home / "state" / "recaps.jsonl", 1)[0]
    )
    assert record["status"] == "completed"
    assert record["toolNames"] == ["reader"]
    assert record["outputExcerpt"] == "done"


def _wait_one(path: Path) -> list[str]:
    return wait_for_recap_lines(path, 1)


def _wait_two(path: Path) -> list[str]:
    return wait_for_recap_lines(path, 2)


def test_reoffer_during_a_live_turn_writes_exactly_one_record(
    tmp_path: Path,
) -> None:
    """A second offer of the still-pending row must not duplicate the recap."""

    home = tmp_path / "home"
    write_hooks_config(home)
    bus, service, consumer = build_service(home)
    prime_configuration(service)
    started = threading.Event()
    release = threading.Event()
    process = StreamingTurnProcess(
        harness="test",
        label="test",
        client_factory=lambda: _HeldTurnClient(started, release),
        thread_name="recap-turn-reoffer",
        on_turn_completed=service.observe_turn,
    )
    delivery = HarnessDelivery(
        "delivery-1", "conversation-1", "agent:owner:node:sender", RECIPIENT, "body"
    )
    try:
        assert process.wait_ready(timeout=1)
        assert process.enqueue(delivery)
        assert started.wait(1)
        # The production tick would offer the still-pending row again.
        assert not process.enqueue(delivery)
        release.set()
        deadline = time.monotonic() + 1
        results = ()
        while not results:
            results = process.drain_results()
            assert time.monotonic() < deadline
            time.sleep(0.005)
    finally:
        release.set()
        process.stop()
        assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
        assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
    record = json.loads(
        wait_for_recap_lines(home / "state" / "recaps.jsonl", 1)[0]
    )
    assert record["deliveryId"] == "delivery-1"
    assert record["toolNames"] == ["reader"]
