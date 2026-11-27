"""The startup answer and the periodic load are bounded per grantor, per org and by a deadline.

Decision (service grant S5 fix, 2026-11-27, critic 3): a grant scan never
reads without bound. Over a limit or out of time is "unknown", never "no
grant": the startup answer is "do not start" unless another grant already
answers yes, and the periodic path keeps its previous result. Each emits one
fixed diagnostic event, ``service.grant.scan_limited``.

Decision (coordinator ruling, 2026-11-27, revising the first fix): the
document and byte limits apply to each grantor's directory on its own (256
documents, 512 KiB), so one member writing junk documents only makes their
own grants unknown. Such a grantor keeps their previous result and the event
names them; every other grantor of the org is read as usual, new grants and
revocations included. The org keeps a limit on how many grantor directories
it may hold (1024) and its deadline; those two still make the whole org
unknown, and their event names no grantor.

The limits are module constants read at call time, so these tests lower them
instead of writing hundreds of documents (one OrgFS write costs ~35 ms).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.service_grants import queries
from hyprial.daemon.impl.service_grants.consumer import GrantConsumer, has_effective_grant
from hyprial.daemon.impl.service_grants.consumer import keys as keys_module
from hyprial.daemon.impl.service_grants.consumer import maintenance as maintenance_module
from hyprial.daemon.impl.service_grants.consumer import startup as startup_module
from hyprial.identity import org_space_name, read_user_provider_value
from tests.daemon.service_grants.consumer.support import KEY_FIELD, ORG, SECRET, as_author, grantor
from tests.daemon.service_grants.consumer_fixes.support import close, consumer_arguments, world_at

SCAN_LIMITED = "service.grant.scan_limited"
PORTS = (8447, 8448, 8449)
CAROL_PORTS = (9447, 9448)


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


def carol_grants(world: dict[str, Any], grant_id: str, *, name: str, port: int, revoked: bool = False) -> None:
    """Carol, a second grantor of the org, writes a grant to "@members" by hand."""

    with as_author(world["runtime"], "carol"):
        world["store"].put_service_grant(
            ORG, "carol", grant_id,
            {
                "schemaVersion": 1,
                "grantId": grant_id,
                "service": {"deviceId": "device-c", "port": port, "name": name, "protocol": "tcp", "note": None},
                "recipient": "@members",
                "grantedAtMs": 1,
                "expiresAtMs": None,
                "revokedAtMs": 2 if revoked else None,
                "credential": None,
            },
        )


CAROL_FIRST = "sg.cccccccccccccccccccccccccc"
CAROL_SECOND = "sg.dddddddddddddddddddddddddd"


class Steps:
    """A monotonic clock that stands still for ``still`` readings, then jumps far ahead."""

    def __init__(self, still: int) -> None:
        self.still = still
        self.readings = 0

    def __call__(self) -> float:
        self.readings += 1
        return 0.0 if self.readings <= self.still else 1_000.0


def count_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    reads: list[tuple[str, str]] = []
    original = OrgFsDirectoryStore.read_service_grant

    def counting(self: Any, org: str, owner: str, grant_id: str) -> Any:
        reads.append((owner, grant_id))
        return original(self, org, owner, grant_id)

    monkeypatch.setattr(OrgFsDirectoryStore, "read_service_grant", counting)
    return reads


def listed_bytes(world: dict[str, Any], owner: str) -> int:
    facade = world["runtime"].facade
    [space] = [space for space in facade.spaces() if space.name == org_space_name(ORG)]
    return sum(
        node.size or 0
        for node in facade.listdir(space.space_id, f"directory/service-grants/{owner}", recursive=True)
        if node.kind == "doc"
    )


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


def entry_names(source: GrantConsumer) -> list[str]:
    return sorted(item.name for item in source.entries())


def test_the_limits_are_the_ruled_values() -> None:
    assert queries.MAX_GRANTOR_GRANT_DOCUMENTS == 256
    assert queries.GRANTOR_READ_BYTES_LIMIT == 512 * 1024
    assert queries.MAX_ORG_GRANTORS == 1024


def test_a_scan_within_the_limits_equals_the_default_load(world: dict[str, Any]) -> None:
    three_grants(world)
    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    store = world["store"]

    scan = queries.scan_org_service_grants(store, ORG, queries.ScanLimits(world["runtime"].facade))

    assert scan.grants == queries.load_org_service_grants(store, ORG)
    assert scan.limited == {}
    assert len(scan.grants) == 4
    assert queries.scan_org_service_grants(
        store, ORG, queries.ScanLimits(world["runtime"].facade), owner="alice"
    ).grants == queries.load_org_service_grants(store, ORG, owner="alice")


@pytest.mark.parametrize("reason", ["documents", "bytes"])
def test_a_grantor_over_a_limit_is_left_unread_and_named_while_the_others_are_read(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    three_grants(world)
    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    if reason == "documents":
        bound = {"max_documents": 2}
    else:
        assert listed_bytes(world, "carol") < listed_bytes(world, "alice")
        bound = {"max_bytes": listed_bytes(world, "carol")}
    reads = count_reads(monkeypatch)

    scan = queries.scan_org_service_grants(
        world["store"], ORG, queries.ScanLimits(world["runtime"].facade, **bound)
    )

    assert scan.limited == {"alice": reason}
    assert [item.grantor for item in scan.grants] == ["carol"]
    assert reads == [("carol", CAROL_FIRST)]


def test_too_many_grantor_directories_limit_the_whole_org_before_any_read(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    three_grants(world)
    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    reads = count_reads(monkeypatch)

    with pytest.raises(queries.GrantScanLimited) as limited:
        queries.scan_org_service_grants(
            world["store"], ORG, queries.ScanLimits(world["runtime"].facade, max_grantors=1)
        )

    assert limited.value.reason == "grantors"
    assert reads == []


def test_the_deadline_reaches_the_document_reads(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    three_grants(world)
    reads = count_reads(monkeypatch)
    clock = Steps(still=3)

    with pytest.raises(queries.GrantScanLimited) as limited:
        queries.scan_org_service_grants(
            world["store"], ORG, queries.ScanLimits(world["runtime"].facade, deadline=0.5, clock=clock)
        )

    assert limited.value.reason == "deadline"
    assert 0 < len(reads) < 3


def test_the_default_load_reads_no_clock_and_has_no_limit(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """S4-py calls the loader without limits; that call keeps today's behaviour."""

    three_grants(world)
    monkeypatch.setattr(queries, "MAX_GRANTOR_GRANT_DOCUMENTS", 1)
    monkeypatch.setattr(queries, "GRANTOR_READ_BYTES_LIMIT", 1)
    monkeypatch.setattr(queries, "MAX_ORG_GRANTORS", 0)

    assert len(queries.load_org_service_grants(world["store"], ORG)) == 3


def test_startup_with_the_only_grantor_over_a_limit_answers_do_not_start_and_names_them(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    three_grants(world)
    assert startup_answer(world) is True
    monkeypatch.setattr(startup_module, "MAX_GRANTOR_GRANT_DOCUMENTS", 2)

    assert startup_answer(world) is False
    assert world["events"].of(SCAN_LIMITED) == [
        {"org": ORG, "phase": "startup", "reason": "documents", "grantor": "alice"}
    ]


def test_startup_still_finds_another_grantors_grant_past_a_limited_one(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    three_grants(world)
    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    monkeypatch.setattr(startup_module, "MAX_GRANTOR_GRANT_DOCUMENTS", 2)

    assert startup_answer(world) is True


def test_startup_with_too_many_grantors_answers_do_not_start(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    three_grants(world)
    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    monkeypatch.setattr(startup_module, "MAX_ORG_GRANTORS", 1)

    assert startup_answer(world) is False
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "startup", "reason": "grantors"}]


def test_startup_out_of_time_inside_one_org_answers_do_not_start(world: dict[str, Any]) -> None:
    """Before the fix the deadline was only read between orgs, so one large org ran unbounded."""

    three_grants(world)

    assert startup_answer(world, monotonic=Steps(still=3)) is False
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "startup", "reason": "deadline"}]


def test_periodic_a_grantor_over_a_limit_keeps_their_previous_result_and_says_why_once(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    side = grantor(world, world["clock"], ports=PORTS)
    side.grant(org=ORG, port=PORTS[0], device="bob/device-b", name="magpie")
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    assert entry_names(source) == ["magpie"]

    side.grant(org=ORG, port=PORTS[1], device="bob/device-b", name="relay")
    side.grant(org=ORG, port=PORTS[2], device="bob/device-b", name="otter")
    monkeypatch.setattr(maintenance_module, "MAX_GRANTOR_GRANT_DOCUMENTS", 2)
    for _ in range(3):
        source.refresh()

    assert entry_names(source) == ["magpie"]
    assert world["events"].of(SCAN_LIMITED) == [
        {"org": ORG, "phase": "periodic", "reason": "documents", "grantor": "alice"}
    ]


def test_another_grantors_new_grant_and_revocation_are_seen_while_one_grantor_is_over_a_limit(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ruling's case: alice's junk does not freeze carol's grants."""

    side = grantor(world, world["clock"], ports=PORTS)
    side.grant(org=ORG, port=PORTS[0], device="bob/device-b", name="magpie")
    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    assert entry_names(source) == ["harbor", "magpie"]

    side.grant(org=ORG, port=PORTS[1], device="bob/device-b", name="relay")
    side.grant(org=ORG, port=PORTS[2], device="bob/device-b", name="otter")
    monkeypatch.setattr(maintenance_module, "MAX_GRANTOR_GRANT_DOCUMENTS", 2)
    source.refresh()
    assert entry_names(source) == ["harbor", "magpie"]

    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0], revoked=True)
    carol_grants(world, CAROL_SECOND, name="lantern", port=CAROL_PORTS[1])
    source.refresh()

    assert entry_names(source) == ["lantern", "magpie"]
    assert world["events"].of(SCAN_LIMITED) == [
        {"org": ORG, "phase": "periodic", "reason": "documents", "grantor": "alice"}
    ]


def test_a_limited_grantor_with_no_earlier_answer_derives_nothing_and_keeps_their_key(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a restart there is no previous result; "unknown" must still not withdraw the Key."""

    grant_id = grantor(world, world["clock"]).grant(
        org=ORG, port=PORTS[0], device="bob/device-b", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )["grant"]["grantId"]
    entry_id = keys_module.entry_id_for_grant(grant_id)
    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    GrantConsumer(**consumer_arguments(world)).refresh()
    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == SECRET

    assert listed_bytes(world, "carol") < listed_bytes(world, "alice")
    monkeypatch.setattr(maintenance_module, "GRANTOR_READ_BYTES_LIMIT", listed_bytes(world, "carol"))
    restarted = GrantConsumer(**consumer_arguments(world))
    restarted.refresh()

    assert entry_names(restarted) == ["harbor"]
    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == SECRET
    assert restarted.owns_entry(entry_id)
    assert world["events"].of(SCAN_LIMITED) == [
        {"org": ORG, "phase": "periodic", "reason": "bytes", "grantor": "alice"}
    ]


def test_periodic_with_too_many_grantors_keeps_the_previous_result(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    side = grantor(world, world["clock"], ports=PORTS)
    side.grant(org=ORG, port=PORTS[0], device="bob/device-b", name="magpie")
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()

    carol_grants(world, CAROL_FIRST, name="harbor", port=CAROL_PORTS[0])
    monkeypatch.setattr(maintenance_module, "MAX_ORG_GRANTORS", 1)
    for _ in range(2):
        source.refresh()

    assert entry_names(source) == ["magpie"]
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "periodic", "reason": "grantors"}]


def test_periodic_out_of_time_keeps_the_previous_result(world: dict[str, Any]) -> None:
    side = grantor(world, world["clock"], ports=PORTS)
    side.grant(org=ORG, port=PORTS[0], device="bob/device-b", name="magpie")
    clock = Steps(still=10_000)
    source = GrantConsumer(**consumer_arguments(world), monotonic=clock)
    source.refresh()

    side.grant(org=ORG, port=PORTS[1], device="bob/device-b", name="relay")
    clock.still = clock.readings + 2
    source.refresh()

    assert entry_names(source) == ["magpie"]
    assert world["events"].of(SCAN_LIMITED) == [{"org": ORG, "phase": "periodic", "reason": "deadline"}]
