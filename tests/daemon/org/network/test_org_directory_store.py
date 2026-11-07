"""OrgFsDirectoryStore against a real local OrgFS runtime (no transport)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from threading import Event, Thread
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.identity import protected_directory_doc_id
from hyprial.identity.impl.org_directory import (
    DirectoryDevice,
    INVITES_DIR,
    directory_binding_path,
)


def _device(device_id: str, owner: str = "owner@example") -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=owner,
        server_public=f"nodekey:server-{device_id}",
        client_public=f"nodekey:client-{device_id}",
        address=f"tcp://127.0.0.1:7447#{device_id}-psk",
        key_generation=1,
        updated_at="2026-10-03T12:00:00+00:00",
    )


def _unverified_proof(*, expires_at: int) -> str:
    encode = lambda value: base64.urlsafe_b64encode(  # noqa: E731 - compact fixture
        json.dumps(value).encode()
    ).rstrip(b"=").decode()
    return f"{encode({'alg': 'RS256'})}.{encode({'exp': expires_at})}.signature"


def _binding(user: str, *, expires_at: int = 1) -> dict[str, str]:
    return {
        "user": user,
        "larkUnionId": f"on_{user.split('@', 1)[0]}",
        "proof": _unverified_proof(expires_at=expires_at),
        "publishedAt": "2026-10-07T00:00:00+00:00",
    }


@contextmanager
def _as(runtime: OrgFsRuntime, owner: str):
    previous = runtime.facade.author
    runtime.facade.author = f"user:{owner}"
    try:
        yield
    finally:
        runtime.facade.author = previous


@pytest.fixture
def runtime(tmp_path: Path) -> OrgFsRuntime:
    runtime = OrgFsRuntime(
        tmp_path / "state", node_id="node-a", author="user:owner@example"
    )
    yield runtime
    runtime.close()


def test_put_and_list_devices_round_trip(runtime: OrgFsRuntime) -> None:
    store = OrgFsDirectoryStore(runtime.facade)
    store.orgs() == []

    space = runtime.facade.create_space("group-acme")
    store.ensure_org_directory_roots("group-acme")
    store.put_device("group-acme", _device("node-a"))
    runtime.facade.invite(space.space_id, "user:bob@example", "rw")
    with _as(runtime, "bob@example"):
        store.put_device("group-acme", _device("node-b", owner="bob@example"))

    listed = store.list_devices("group-acme")
    assert [device.device_id for device in listed] == ["node-a", "node-b"]
    assert listed[0] == _device("node-a")
    assert listed[1].owner == "bob@example"
    assert runtime.facade.stat(
        space.space_id, "directory/devices/bob@example/node-b.json"
    ).kind == "doc"
    # put is an upsert keyed by device_id.
    store.put_device("group-acme", _device("node-a", owner="owner@example"))
    assert len(store.list_devices("group-acme")) == 2


def test_orgs_lists_only_org_spaces(runtime: OrgFsRuntime) -> None:
    store = OrgFsDirectoryStore(runtime.facade)
    runtime.facade.create_space("personal")
    runtime.facade.create_space("group-zeta")
    runtime.facade.create_space("group-alpha")
    assert store.orgs() == ["group-alpha", "group-zeta"]


def test_put_binding_refuses_a_still_live_proof_as_last_line(
    runtime: OrgFsRuntime,
) -> None:
    runtime.facade.create_space("group-acme")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")

    with pytest.raises(ValueError, match="not expired"):
        store.put_binding(
            "group-acme",
            _binding("owner@example", expires_at=int(time.time()) + 3_600),
        )

    assert store.list_binding_rows("group-acme") == []


def test_raw_orgfs_write_refuses_a_still_live_binding_proof(
    runtime: OrgFsRuntime,
) -> None:
    space = runtime.facade.create_space("group-acme")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    path = directory_binding_path("owner@example")
    runtime.facade.mkdir(space.space_id, path.rsplit("/", 1)[0])

    with pytest.raises(OrgFsError, match="not-a-member"):
        runtime.facade.write_text(
            space.space_id,
            path,
            json.dumps(
                _binding("owner@example", expires_at=int(time.time()) + 3_600)
            ),
        )

    with pytest.raises(OrgFsError) as missing:
        runtime.facade.stat(space.space_id, path)
    assert missing.value.code == "unknown-doc"


def test_binding_reader_requires_exact_user_author(
    runtime: OrgFsRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    space = runtime.facade.create_space("group-acme")
    runtime.facade.invite(space.space_id, "user:bob@example", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    with _as(runtime, "bob@example"):
        store.put_binding("group-acme", _binding("bob@example"))
    monkeypatch.setattr(
        runtime.facade,
        "document_authors",
        lambda _space_id, _node: ("user:owner@example",),
    )

    assert store.list_binding_rows("group-acme") == []


def test_binding_reader_rejects_wrong_protected_document_id(
    runtime: OrgFsRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    space = runtime.facade.create_space("group-acme")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    store.put_binding("group-acme", _binding("owner@example"))
    path = directory_binding_path("owner@example")
    info = runtime.facade.stat(space.space_id, path)
    wrong_doc_id = "doc-00000000-0000-4000-8000-000000000099"
    projection = runtime.facade._spaces[space.space_id]
    projection.contents[wrong_doc_id] = projection.contents[info.doc_id]
    projection.nodes[info.node_id].doc_id = wrong_doc_id
    monkeypatch.setattr(
        runtime.facade,
        "document_authors",
        lambda _space_id, _node: ("user:owner@example",),
    )

    assert store.list_binding_rows("group-acme") == []


def test_list_devices_on_a_fresh_org_is_empty(runtime: OrgFsRuntime) -> None:
    store = OrgFsDirectoryStore(runtime.facade)
    runtime.facade.create_space("group-acme")
    # No device documents yet: an empty directory, not an error.
    assert store.list_devices("group-acme") == []


def test_reader_skips_prefixed_row_when_path_record_and_authors_disagree(
    runtime: OrgFsRuntime,
) -> None:
    """A pre-fix poisoned row is warning-only and never reaches consumers."""

    space = runtime.facade.create_space("group-acme")
    valid = _device("random-device")
    setup = OrgFsDirectoryStore(runtime.facade)
    setup.ensure_org_directory_roots("group-acme")
    setup.put_device("group-acme", valid)
    info = runtime.facade.stat(
        space.space_id, "directory/devices/owner@example/random-device.json"
    )
    poisoned = replace(valid, owner="bob@example")
    runtime.facade._spaces[space.space_id].contents[info.doc_id].set(
        json.dumps(poisoned.as_record(), sort_keys=True, separators=(",", ":"))
    )
    events: list[tuple[str, str, str, dict]] = []
    store = OrgFsDirectoryStore(
        runtime.facade,
        logger=lambda level, domain, event, **fields: events.append(
            (level, domain, event, fields)
        ),
    )

    assert store.list_devices("group-acme") == []
    assert events == [
        (
            "warn",
            "org",
            "directory.device_owner_mismatch",
            {
                "path": "directory/devices/owner@example/random-device.json",
                "pathPrincipal": "user:owner@example",
                "recordPrincipal": "user:bob@example",
                "docAuthors": ["user:owner@example"],
            },
        )
    ]


def test_reader_fails_closed_for_public_key_reuse_across_owners(
    runtime: OrgFsRuntime,
) -> None:
    """One node key cannot authorize or route two different principals."""

    space = runtime.facade.create_space("group-acme")
    runtime.facade.invite(space.space_id, "user:bob@example", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    owner = _device("owner-device")
    store.put_device("group-acme", owner)
    with _as(runtime, "bob@example"):
        store.put_device(
            "group-acme",
            replace(
                _device("bob-device", owner="bob@example"),
                server_public=owner.server_public,
            ),
        )
    events: list[tuple[str, str, str, dict]] = []
    reader = OrgFsDirectoryStore(
        runtime.facade,
        logger=lambda level, domain, event, **fields: events.append(
            (level, domain, event, fields)
        ),
    )

    assert reader.list_devices("group-acme") == []
    assert events == [
        (
            "warn",
            "org",
            "directory.device_key_conflict",
            {
                "key": owner.server_public,
                "keyFields": ["serverPublic"],
                "owners": ["user:bob@example", "user:owner@example"],
            },
        )
    ]


def test_malformed_device_rows_warn_and_do_not_hide_other_peers(
    runtime: OrgFsRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-E: each bad row is isolated from list and peer projection."""

    space = runtime.facade.create_space("group-acme")
    runtime.facade.invite(space.space_id, "user:bob@example", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    good = _device("node-a")
    store.put_device("group-acme", good)
    with _as(runtime, "bob@example"):
        for device_id, payload in (
            ("broken-text", "not json"),
            ("broken-record", '{"deviceId":"broken-record"}'),
        ):
            store.put_device("group-acme", _device(device_id, owner="bob@example"))
            runtime.facade.write_text(
                space.space_id,
                f"directory/devices/bob@example/{device_id}.json",
                payload,
            )
    events: list[tuple[str, str, str, dict]] = []
    reader = OrgFsDirectoryStore(
        runtime.facade,
        logger=lambda level, domain, event, **fields: events.append(
            (level, domain, event, fields)
        ),
    )
    bad_node_ids = {
        runtime.facade.stat(
            space.space_id,
            f"directory/devices/bob@example/{device_id}.json",
        ).node_id
        for device_id in ("broken-text", "broken-record")
    }
    bad_reads = 0
    original_read = runtime.facade.read_text

    def count_bad_reads(space_id: str, node: str):
        nonlocal bad_reads
        if node.removeprefix("id:") in bad_node_ids:
            bad_reads += 1
        return original_read(space_id, node)

    monkeypatch.setattr(runtime.facade, "read_text", count_bad_reads)

    devices, scan = reader.scan_devices("group-acme")
    assert devices == [good]
    assert {problem["reason"] for problem in scan["dataProblems"]} == {
        "invalid-device-record"
    }
    assert {problem["deviceId"] for problem in scan["dataProblems"]} == {
        "broken-text",
        "broken-record",
    }
    assert bad_reads == 2
    # Directory diagnostics retain quarantine; they no longer select network peers.
    assert reader.list_devices("group-acme") == [good]
    invalid = [
        fields
        for _level, _domain, event, fields in events
        if event == "directory.device_invalid"
    ]
    assert len(invalid) == 2, "one report per bad row on the first scan"
    assert {fields["path"].rsplit("/", 1)[-1] for fields in invalid} == {
        "broken-text.json",
        "broken-record.json",
    }
    assert all(fields["fingerprint"] for fields in invalid)

    # The same unreadable bytes must not be re-reported on the next tick: a scan
    # runs every reconcile cycle, and re-reading one corrupt row at tick rate
    # turned a single bad byte into a standing load source (measured at 41
    # reports/minute on a live node).  Isolation, not reporting, keeps the other
    # peers visible.
    assert reader.list_devices("group-acme") == [good]
    assert bad_reads == 2, "quarantined versions are not parsed again before retry"
    assert [event for _level, _domain, event, _fields in events].count(
        "directory.device_invalid"
    ) == 2, "identical bytes are quiet on the next scan"


def test_a_changed_device_row_reports_again(
    runtime: OrgFsRuntime, tmp_path: Path
) -> None:
    """Counterexample: the quiet window keys on content, never on the path."""

    space = runtime.facade.create_space("group-acme")
    runtime.facade.invite(space.space_id, "user:bob@example", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    with _as(runtime, "bob@example"):
        store.put_device("group-acme", _device("flapping", owner="bob@example"))
        runtime.facade.write_text(
            space.space_id,
            "directory/devices/bob@example/flapping.json",
            "not json at all",
        )
    events: list[tuple[str, str, str, dict]] = []
    reader = OrgFsDirectoryStore(
        runtime.facade,
        logger=lambda level, domain, event, **fields: events.append(
            (level, domain, event, fields)
        ),
    )

    assert reader.list_devices("group-acme") == []
    assert reader.list_devices("group-acme") == []
    assert len(
        [1 for _l, _d, event, _f in events if event == "directory.device_invalid"]
    ) == 1

    with _as(runtime, "bob@example"):
        runtime.facade.write_text(
            space.space_id,
            "directory/devices/bob@example/flapping.json",
            "different bytes, still not json",
        )

    assert reader.list_devices("group-acme") == []
    invalid = [
        fields
        for _level, _domain, event, fields in events
        if event == "directory.device_invalid"
    ]
    assert len(invalid) == 2, "changed bytes are reported at once"
    assert invalid[1]["suppressedSinceLastReport"] == 1


def test_shared_quarantine_is_safe_during_a_concurrent_repair(
    runtime: OrgFsRuntime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    space = runtime.facade.create_space("group-acme")
    setup = OrgFsDirectoryStore(runtime.facade)
    setup.ensure_org_directory_roots("group-acme")
    repaired = _device("node-a")
    valid = _device("node-b")
    setup.put_device("group-acme", repaired)
    setup.put_device("group-acme", valid)
    path = "directory/devices/owner@example/node-a.json"
    runtime.facade.write_text(space.space_id, path, "not json")

    quarantine = InvalidDeviceQuarantine()
    first = OrgFsDirectoryStore(runtime.facade, invalid_devices=quarantine)
    second = OrgFsDirectoryStore(runtime.facade, invalid_devices=quarantine)
    noted = Event()
    repair_scanned = Event()
    original_note = first._note_invalid_device

    def pause_after_note(*args, **kwargs):
        result = original_note(*args, **kwargs)
        noted.set()
        assert repair_scanned.wait(2.0)
        return result

    monkeypatch.setattr(first, "_note_invalid_device", pause_after_note)
    first_devices = []
    failures: list[BaseException] = []

    def project_invalid_version() -> None:
        try:
            first_devices.extend(first.list_devices("group-acme"))
        except BaseException as error:  # noqa: BLE001 - preserve thread failure
            failures.append(error)

    thread = Thread(target=project_invalid_version)
    thread.start()
    assert noted.wait(2.0)
    setup.put_device("group-acme", repaired)
    assert second.list_devices("group-acme") == [repaired, valid]
    repair_scanned.set()
    thread.join(2.0)

    assert not thread.is_alive()
    assert failures == []
    assert first_devices == [valid]


def test_invalid_device_retry_uses_capped_exponential_backoff(
    runtime: OrgFsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network import directory as directory_module

    clock = [100.0]
    monkeypatch.setattr(directory_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(directory_module, "_INVALID_DEVICE_QUIET_SECONDS", 10.0)
    monkeypatch.setattr(directory_module, "_INVALID_DEVICE_RETRY_MAX_SECONDS", 20.0)
    reader = OrgFsDirectoryStore(runtime.facade)
    error = ValueError("bad row")

    assert reader._note_invalid_device("space", "bad.json", "bad", error)
    first_retry = reader._invalid_device_quiet[("space", "bad.json")][1]
    assert first_retry == 110.0
    clock[0] = first_retry
    assert reader._note_invalid_device("space", "bad.json", "bad", error)
    second_retry = reader._invalid_device_quiet[("space", "bad.json")][1]
    assert second_retry == 130.0
    clock[0] = second_retry
    assert reader._note_invalid_device("space", "bad.json", "bad", error)
    assert reader._invalid_device_quiet[("space", "bad.json")][1] == 150.0


def test_the_quiet_map_is_hard_bounded(
    runtime: OrgFsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quiet map must not grow with a remote directory's contents.

    Its keys come from a remote org directory, so an unbounded cache would grow
    with whatever that directory happens to contain.  The bound drops the
    oldest insertion, and a dropped row simply reports again -- the cache is a
    reporting aid, never a correctness gate.
    """

    from hyprial.daemon.impl.org.network import directory as directory_module

    monkeypatch.setattr(directory_module, "_INVALID_DEVICE_QUIET_MAX", 3)
    events: list[tuple[str, str, str, dict]] = []
    reader = OrgFsDirectoryStore(
        runtime.facade,
        logger=lambda level, domain, event, **fields: events.append(
            (level, domain, event, fields)
        ),
    )
    error = ValueError("bad row")

    for index in range(5):
        assert reader._note_invalid_device("group-acme", f"p{index}.json", "x", error)
    assert len(reader._invalid_device_quiet) == 3, "the map never exceeds the cap"
    assert set(reader._invalid_device_quiet) == {
        ("group-acme", "p2.json"),
        ("group-acme", "p3.json"),
        ("group-acme", "p4.json"),
    }, "the oldest insertions are the ones dropped"

    # A dropped row reports again: eviction loses quiet, never the report.
    assert reader._note_invalid_device("group-acme", "p0.json", "x", error)
    assert len(
        [1 for _l, _d, event, _f in events if event == "directory.device_invalid"]
    ) == 6


def test_the_quiet_window_is_per_space_not_per_path(
    runtime: OrgFsRuntime,
) -> None:
    """Counterexample: one org's bad bytes must not silence another org's.

    The same path with the same bytes can exist in two org spaces.  A
    path-only key would report the first and swallow the second, which hides a
    real second failure behind an unrelated first one.
    """

    events: list[tuple[str, str, str, dict]] = []
    reader = OrgFsDirectoryStore(
        runtime.facade,
        logger=lambda level, domain, event, **fields: events.append(
            (level, domain, event, fields)
        ),
    )
    space_ids = {}
    for org in ("group-acme", "group-globex"):
        space = runtime.facade.create_space(org)
        space_ids[org] = space.space_id
        runtime.facade.invite(space.space_id, "user:bob@example", "rw")
        store = OrgFsDirectoryStore(runtime.facade)
        store.ensure_org_directory_roots(org)
        with _as(runtime, "bob@example"):
            store.put_device(org, _device("node-x", owner="bob@example"))
            runtime.facade.write_text(
                space.space_id,
                "directory/devices/bob@example/node-x.json",
                "identical bad bytes in both orgs",
            )

    assert reader.list_devices("group-acme") == []
    assert reader.list_devices("group-globex") == []
    invalid = [
        fields
        for _level, _domain, event, fields in events
        if event == "directory.device_invalid"
    ]
    assert len(invalid) == 2, "each space reports its own first sighting"
    assert {fields["space"] for fields in invalid} == set(space_ids.values())


def test_reader_ignores_a_non_deterministic_doc_link_with_warning(
    runtime: OrgFsRuntime,
) -> None:
    """R3-H: a protected path never adopts a random physical doc id."""

    space = runtime.facade.create_space("group-acme")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    store.put_device("group-acme", _device("node-a"))
    info = runtime.facade.stat(
        space.space_id, "directory/devices/owner@example/node-a.json"
    )
    runtime.facade._spaces[space.space_id].nodes[info.node_id].doc_id = (
        "doc-00000000-0000-4000-8000-000000000099"
    )
    events: list[tuple[str, str, str, dict]] = []
    reader = OrgFsDirectoryStore(
        runtime.facade,
        logger=lambda level, domain, event, **fields: events.append(
            (level, domain, event, fields)
        ),
    )

    assert reader.list_devices("group-acme") == []
    assert events[0][2] == "directory.protected_doc_id_mismatch"


def test_put_device_reuses_the_deterministic_document_id(
    runtime: OrgFsRuntime,
) -> None:
    """Round 3 removes M2 replacement: authorization prevents pollution."""

    space = runtime.facade.create_space("group-acme")
    store = OrgFsDirectoryStore(runtime.facade)
    store.ensure_org_directory_roots("group-acme")
    clean = _device("node-a")
    store.put_device("group-acme", clean)
    path = "directory/devices/owner@example/node-a.json"
    original = runtime.facade.stat(space.space_id, path)
    assert original.doc_id is not None

    store.put_device("group-acme", clean)

    replacement = runtime.facade.stat(space.space_id, path)
    assert replacement.doc_id == original.doc_id
    assert runtime.stores[space.space_id].document_authors(replacement.doc_id) == (
        "user:owner@example",
    )
    assert store.list_devices("group-acme") == [clean]


def test_unknown_org_raises_unknown_space(runtime: OrgFsRuntime) -> None:
    store = OrgFsDirectoryStore(runtime.facade)
    with pytest.raises(OrgFsError) as raised:
        store.list_devices("group-nosuch")
    assert raised.value.code == "unknown-space"


def test_pending_invite_document_lands_in_the_invites_dir(runtime: OrgFsRuntime) -> None:
    store = OrgFsDirectoryStore(runtime.facade)
    runtime.facade.create_space("group-acme")
    store.put_invite("group-acme", "digest-1", {"org": "group-acme", "tokenDigest": "digest-1"})
    space_id = next(
        space.space_id for space in runtime.facade.spaces() if space.name == "group-acme"
    )
    entries = runtime.facade.listdir(space_id, INVITES_DIR)
    assert [entry.name for entry in entries] == ["digest-1.json"]
    text, _version = runtime.facade.read_text(space_id, f"id:{entries[0].node_id}")
    assert '"tokenDigest":"digest-1"' in text


def test_leave_sweep_drops_an_unparseable_member_written_name() -> None:
    """Review 1022 a: the sweep's fallback for a name that cannot be a user.

    Protected leave ids refuse such names at write time, so no store can
    produce one today; the fallback is defence in depth for rows written
    before that rule, driven here directly.
    """

    from hyprial.daemon.impl.org.network.directory import leave_request_admitted
    from hyprial.daemon.impl.orgfs.api import OrgFsError

    events: list[str] = []

    def log(_level: str, _component: str, event: str, **_fields: object) -> None:
        events.append(event)

    class _Store:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.removed: list[tuple[str, str]] = []

        def remove_leave_request(self, org: str, user: str) -> None:
            if self.fail:
                raise OrgFsError("not-a-member", {"reason": "cannot remove"})
            self.removed.append((org, user))

    store = _Store(fail=False)
    assert leave_request_admitted(store, log, "group-acme", "a:b") is False
    assert store.removed == [("group-acme", "a:b")]
    assert events == ["org.leaves.invalid-request"]

    events.clear()
    stuck = _Store(fail=True)
    assert leave_request_admitted(stuck, log, "group-acme", " mallory") is False
    assert events == [
        "org.leaves.invalid-request",
        "org.leaves.invalid-request-not-removed",
    ]


REAL_ORG = "group-internal"
REAL_OWNER = "bob.smith@example.com"
REAL_HQ_DEVICE = "node-hq-01.example-tailnet.ts.net"
REAL_INVITEES = (
    "amy.williams@example.com",
    "dana.fisher@example.com",
    "chloe.martin@example.com",
    "jo.carter@example.com",
)


def _real_device(device_id: str, owner: str) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=owner,
        server_public=f"nodekey:server-{device_id}",
        client_public=f"nodekey:client-{device_id}",
        address=f"tcp://127.0.0.1:7447#{device_id}-psk",
        key_generation=1,
        updated_at="2026-10-08T00:00:00+00:00",
    )


def _open_real(state: Path) -> OrgFsRuntime:
    return OrgFsRuntime(state, node_id=REAL_HQ_DEVICE, author=f"user:{REAL_OWNER}")


def test_invites_member_grants_and_device_rows_with_real_names(tmp_path: Path) -> None:
    """Real-length names (2026-10-08 hq): the invite id is past the old 240 cap.

    Every earlier test used names like ``alice`` and ``group-demo``; with the
    real owner the invite document's protected id is 253 characters and
    ``org invite`` failed for all four invitees.
    """

    state = tmp_path / "state"
    runtime = _open_real(state)
    try:
        store = OrgFsDirectoryStore(runtime.facade)
        space = runtime.facade.create_space(REAL_ORG)
        store.ensure_org_directory_roots(REAL_ORG)
        store.put_device(REAL_ORG, _real_device(REAL_HQ_DEVICE, REAL_OWNER))
        token_ids = {}
        for user in REAL_INVITEES:
            token_id = hashlib.sha256(f"token-for-{user}".encode()).hexdigest()
            token_ids[user] = token_id
            store.put_invite(REAL_ORG, token_id, {"org": REAL_ORG, "user": f"user:{user}"})
        invite_id = protected_directory_doc_id(
            space.space_id,
            f"{INVITES_DIR}/{token_ids[REAL_INVITEES[0]]}.json",
            space_owner=f"user:{REAL_OWNER}",
        )
        # The test only means something past the old 240-character cap.
        assert invite_id is not None and len(invite_id) > 240
        for user in REAL_INVITEES:
            runtime.facade.invite(space.space_id, f"user:{user}", "rw")
            with _as(runtime, user):
                store.put_device(REAL_ORG, _real_device(f"{user.split('@')[0]}-mbp.example-tailnet.ts.net", user))
    finally:
        runtime.close()

    reopened = _open_real(state)
    try:
        store = OrgFsDirectoryStore(reopened.facade)
        owners = sorted(device.owner for device in store.list_devices(REAL_ORG))
        assert owners == sorted((REAL_OWNER, *REAL_INVITEES))
        entries = reopened.facade.listdir(space.space_id, INVITES_DIR)
        assert sorted(entry.name for entry in entries) == sorted(
            f"{token_id}.json" for token_id in token_ids.values()
        )
        for entry in entries:
            text, _version = reopened.facade.read_text(space.space_id, f"id:{entry.node_id}")
            assert f'"org":"{REAL_ORG}"' in text
    finally:
        reopened.close()


def test_directory_scan_does_not_replay_commit_history(
    runtime: OrgFsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = runtime.facade.create_space("group-acme")
    journal = Path(runtime.state_dir) / "orgfs" / "spaces" / space.space_id / "journal.sqlite3"
    db = sqlite3.connect(journal)
    try:
        db.executemany(
            "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, "
            "committed_at, broadcast_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "node-a",
                    index,
                    "tree-bulk-history",
                    b"{}",
                    b"",
                    "2026-10-29T00:00:00+00:00",
                    "2026-10-29T00:00:00+00:00",
                )
                for index in range(300_000)
            ),
        )
        db.commit()
    finally:
        db.close()

    calls = 0
    real_decode = LocalSpaceStore._decode_envelope

    def counting_decode(self, raw):
        nonlocal calls
        calls += 1
        return real_decode(self, raw)

    monkeypatch.setattr(LocalSpaceStore, "_decode_envelope", counting_decode)
    start = time.perf_counter()
    devices, pending = OrgFsDirectoryStore(runtime.facade).scan_devices("group-acme")
    elapsed = time.perf_counter() - start

    assert devices == []
    assert pending == {"contentPending": [], "parkedCommits": []}
    assert calls == 0, "directory scan replayed committed history"
    assert elapsed < 1.0, f"directory scan took {elapsed:.2f}s with 300k commits"
