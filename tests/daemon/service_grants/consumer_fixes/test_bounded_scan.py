"""The startup answer and the periodic load are bounded per org and by a deadline.

Decision (service grant S5 fix, 2026-11-27, critic 3): an org may hold at most
1024 grant documents and one scan may read at most a fixed number of bytes;
the startup deadline reaches the listing and every document read. Over a
limit or out of time is "unknown", never "no grant": the startup answer is
"do not start" and the periodic path keeps its previous result. Each emits
one fixed diagnostic event, ``service.grant.scan-limited``, naming the org,
the phase and which bound was hit, and nothing else.

The limits are module constants read at call time, so these tests lower them
instead of writing a thousand documents (one OrgFS write costs ~35 ms).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.service_grants import queries
from hyprial.daemon.impl.service_grants.consumer import GrantConsumer, has_effective_grant
from hyprial.daemon.impl.service_grants.consumer import maintenance as maintenance_module
from hyprial.daemon.impl.service_grants.consumer import startup as startup_module
from tests.daemon.service_grants.consumer.support import ORG, grantor
from tests.daemon.service_grants.consumer_fixes.support import close, consumer_arguments, world_at

SCAN_LIMITED = "service.grant.scan-limited"
PORTS = (8447, 8448, 8449)


@pytest.fixture
def world(tmp_path: Path, actor_host) -> dict[str, Any]:
    made = world_at(tmp_path, actor_host)
    yield made
    close(made)


def three_grants(world: dict[str, Any]) -> list[str]:
    side = grantor(world, world["clock"], ports=PORTS)
    return [
        side.grant(org=ORG, port=port, device="bob/device-b", name=name)["grant"]["grantId"]
        for port, name in zip(PORTS, ("magpie", "relay", "otter"))
    ]


class Steps:
    """A monotonic clock that stands still for ``still`` readings, then jumps far ahead."""

    def __init__(self, still: int) -> None:
        self.still = still
        self.readings = 0

    def __call__(self) -> float:
        self.readings += 1
        return 0.0 if self.readings <= self.still else 1_000.0


def count_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    original = OrgFsDirectoryStore.read_service_grant

    def counting(self: Any, org: str, owner: str, grant_id: str) -> Any:
        reads.append(grant_id)
        return original(self, org, owner, grant_id)

    monkeypatch.setattr(OrgFsDirectoryStore, "read_service_grant", counting)
    return reads


def startup_answer(world: dict[str, Any], **extra: Any) -> bool:
    return has_effective_grant(
        orgfs=world["runtime"].facade,
        home=world["bob_home"],
        owner="bob",
        device_id="device-b",
        now_ms=world["clock"],
        logger=world["events"],
        **extra,
    )


def test_the_limits_are_the_ruled_values() -> None:
    assert queries.MAX_ORG_GRANT_DOCUMENTS == 1024
    assert queries.SCAN_READ_BYTES_LIMIT == 2 * 1024 * 1024


def test_a_bounded_load_within_the_limits_equals_the_default_load(world: dict[str, Any]) -> None:
    three_grants(world)
    store = world["store"]

    bounded = queries.load_org_service_grants(store, ORG, limits=queries.ScanLimits(world["runtime"].facade))

    assert bounded == queries.load_org_service_grants(store, ORG)
    assert len(bounded) == 3
    assert queries.load_org_service_grants(
        store, ORG, owner="alice", limits=queries.ScanLimits(world["runtime"].facade)
    ) == queries.load_org_service_grants(store, ORG, owner="alice")


@pytest.mark.parametrize(
    ("bound", "reason"),
    [({"max_documents": 2}, "documents"), ({"max_bytes": 100}, "bytes")],
)
def test_a_load_over_a_limit_is_refused_before_any_document_is_read(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, bound: dict[str, int], reason: str
) -> None:
    three_grants(world)
    reads = count_reads(monkeypatch)

    with pytest.raises(queries.GrantScanLimited) as limited:
        queries.load_org_service_grants(
            world["store"], ORG, limits=queries.ScanLimits(world["runtime"].facade, **bound)
        )

    assert limited.value.reason == reason
    assert reads == []


def test_the_deadline_reaches_the_document_reads(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    three_grants(world)
    reads = count_reads(monkeypatch)
    clock = Steps(still=3)

    with pytest.raises(queries.GrantScanLimited) as limited:
        queries.load_org_service_grants(
            world["store"], ORG, limits=queries.ScanLimits(world["runtime"].facade, deadline=0.5, clock=clock)
        )

    assert limited.value.reason == "deadline"
    assert 0 < len(reads) < 3


def test_the_default_load_reads_no_clock_and_has_no_limit(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """S4-py calls the loader without limits; that call keeps today's behaviour."""

    three_grants(world)
    monkeypatch.setattr(queries, "MAX_ORG_GRANT_DOCUMENTS", 1)
    monkeypatch.setattr(queries, "SCAN_READ_BYTES_LIMIT", 1)

    assert len(queries.load_org_service_grants(world["store"], ORG)) == 3


@pytest.mark.parametrize(
    ("constant", "value", "reason"),
    [("MAX_ORG_GRANT_DOCUMENTS", 2, "documents"), ("SCAN_READ_BYTES_LIMIT", 100, "bytes")],
)
def test_startup_over_a_limit_answers_do_not_start_and_says_why_once(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, constant: str, value: int, reason: str
) -> None:
    three_grants(world)
    assert startup_answer(world) is True
    monkeypatch.setattr(startup_module, constant, value)

    assert startup_answer(world) is False
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "startup", "reason": reason}]


def test_startup_out_of_time_inside_one_org_answers_do_not_start(world: dict[str, Any]) -> None:
    """Before the fix the deadline was only read between orgs, so one large org ran unbounded."""

    three_grants(world)

    assert startup_answer(world, monotonic=Steps(still=3)) is False
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "startup", "reason": "deadline"}]


def test_periodic_over_a_limit_keeps_the_previous_result_and_says_why_once(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    side = grantor(world, world["clock"], ports=PORTS)
    side.grant(org=ORG, port=PORTS[0], device="bob/device-b", name="magpie")
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    assert [item.name for item in source.entries()] == ["magpie"]

    side.grant(org=ORG, port=PORTS[1], device="bob/device-b", name="relay")
    side.grant(org=ORG, port=PORTS[2], device="bob/device-b", name="otter")
    monkeypatch.setattr(maintenance_module, "MAX_ORG_GRANT_DOCUMENTS", 2)
    for _ in range(3):
        source.refresh()

    assert [item.name for item in source.entries()] == ["magpie"]
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "periodic", "reason": "documents"}]


def test_periodic_over_a_limit_with_no_earlier_answer_derives_and_withdraws_nothing(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    three_grants(world)
    monkeypatch.setattr(maintenance_module, "SCAN_READ_BYTES_LIMIT", 100)
    source = GrantConsumer(**consumer_arguments(world))

    source.refresh()

    assert source.entries() == ()
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "periodic", "reason": "bytes"}]


def test_periodic_out_of_time_keeps_the_previous_result(world: dict[str, Any]) -> None:
    side = grantor(world, world["clock"], ports=PORTS)
    side.grant(org=ORG, port=PORTS[0], device="bob/device-b", name="magpie")
    clock = Steps(still=10_000)
    source = GrantConsumer(**consumer_arguments(world), monotonic=clock)
    source.refresh()

    side.grant(org=ORG, port=PORTS[1], device="bob/device-b", name="relay")
    clock.still = clock.readings + 2
    source.refresh()

    assert [item.name for item in source.entries()] == ["magpie"]
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "periodic", "reason": "deadline"}]
