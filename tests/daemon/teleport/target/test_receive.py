"""receive on the target: ordering, strict resume, idempotency and undo."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import validate_bundle

from ..stream.samples import make_bundle, stream
from ..support import teleport
from .support import (
    AGENT,
    SESSION,
    TARGET_URI,
    FakeHost,
    Packed,
    chunks,
    error_code,
    feed,
    make_service,
    manifest_digest,
    packed_bundle,
    prepare_request,
    protocol,
    raw_stream,
    receive_request,
    staged,
    staging_entries,
)

HAPPY_CALLS = ["land", "activate", "start"]


def _assert_forgotten(service, host: FakeHost, receiver, operation_id: str, packed: Packed) -> None:
    assert service.state(operation_id) is None
    assert staging_entries(receiver) == []
    assert host.running == set()
    assert host.active == set()
    assert host.landed == set()
    first = chunks(operation_id, packed)[0]
    assert error_code(service.chunk, first) == "TELEPORT_UNKNOWN_OPERATION"
    assert (
        error_code(service.receive, receive_request(operation_id, packed))
        == "TELEPORT_UNKNOWN_OPERATION"
    )


def test_receive_calls_ports_in_plan_order(tmp_path: Path) -> None:
    service, host, _receiver = make_service(tmp_path)
    packed = staged(service, tmp_path)
    assert service.state("op-1") == "staging"
    reply = service.receive(receive_request("op-1", packed))
    assert host.calls == HAPPY_CALLS
    bundle_dir, name, harness, actor = host.land_args[0]
    assert (name, harness, actor) == (AGENT, "claude", AGENT)
    assert host.start_args == [(("--model", "m-1"), SESSION, 5.0)]
    assert reply.operation_id == "op-1"
    assert reply.agent_uri == TARGET_URI
    assert reply.session_ref == SESSION
    assert reply.routines == ()
    assert service.state("op-1") == "received"
    assert host.running == {AGENT}


def test_land_receives_the_validated_unpacked_bundle(tmp_path: Path) -> None:
    seen: list[str] = []

    class CheckingHost(FakeHost):
        def land(self, bundle_dir, *, name, harness):
            manifest = validate_bundle(bundle_dir)
            seen.append(manifest.actor)
            return super().land(bundle_dir, name=name, harness=harness)

    service, _host, _receiver = make_service(tmp_path, CheckingHost())
    packed = staged(service, tmp_path)
    service.receive(receive_request("op-1", packed))
    assert seen == [AGENT]


def test_repeated_receive_returns_first_reply_without_port_calls(tmp_path: Path) -> None:
    service, host, _receiver = make_service(tmp_path)
    packed = staged(service, tmp_path)
    first = service.receive(receive_request("op-1", packed))
    calls = list(host.calls)
    second = service.receive(receive_request("op-1", packed))
    assert second == first
    assert host.calls == calls


def test_null_session_ref_skips_the_session_check(tmp_path: Path) -> None:
    service, host, _receiver = make_service(tmp_path, FakeHost(start_result="fresh-ref"))
    packed = staged(service, tmp_path)
    reply = service.receive(receive_request("op-1", packed, session_ref=None))
    assert host.start_args[0][1] is None
    assert reply.session_ref == "fresh-ref"
    assert service.state("op-1") == "received"


def test_session_ref_mismatch_is_strict_resume_failed_and_undone(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path, FakeHost(start_result="other-ref"))
    packed = staged(service, tmp_path)
    code = error_code(service.receive, receive_request("op-1", packed))
    assert code == "TELEPORT_STRICT_RESUME_FAILED"
    assert host.calls == HAPPY_CALLS + ["stop", "deactivate", "undo_landing"]
    _assert_forgotten(service, host, receiver, "op-1", packed)


PORT_FAILURES = [
    pytest.param(
        "land",
        lambda: teleport().TeleportError("TELEPORT_AGENT_EXISTS", "taken since prepare"),
        "TELEPORT_AGENT_EXISTS",
        ["land"],
        id="land-name-taken",
    ),
    pytest.param(
        "land",
        lambda: RuntimeError("registry refused"),
        "TELEPORT_LANDING_FAILED",
        ["land"],
        id="land-crash",
    ),
    pytest.param(
        "activate",
        lambda: RuntimeError("no runtime context"),
        "TELEPORT_LANDING_FAILED",
        ["land", "activate", "deactivate", "undo_landing"],
        id="activate",
    ),
    pytest.param(
        "start",
        lambda: teleport().TeleportError("TELEPORT_STRICT_RESUME_FAILED", "not ready"),
        "TELEPORT_STRICT_RESUME_FAILED",
        HAPPY_CALLS + ["stop", "deactivate", "undo_landing"],
        id="start-strict",
    ),
    pytest.param(
        "start",
        lambda: RuntimeError("harness crashed"),
        "TELEPORT_STRICT_RESUME_FAILED",
        HAPPY_CALLS + ["stop", "deactivate", "undo_landing"],
        id="start-crash",
    ),
]


@pytest.mark.parametrize(("step", "error", "code", "calls"), PORT_FAILURES)
def test_port_failure_is_undone_in_reverse(
    tmp_path: Path, step: str, error, code: str, calls: list[str]
) -> None:
    host = FakeHost()
    host.failures[step] = error()
    service, _host, receiver = make_service(tmp_path, host)
    packed = staged(service, tmp_path)
    assert error_code(service.receive, receive_request("op-1", packed)) == code
    assert host.calls == calls
    _assert_forgotten(service, host, receiver, "op-1", packed)


def test_failing_undo_step_does_not_stop_the_rest(tmp_path: Path) -> None:
    host = FakeHost()
    host.failures["start"] = RuntimeError("harness crashed")
    host.undo_failures["deactivate"] = RuntimeError("row replaced")
    service, _host, receiver = make_service(tmp_path, host)
    packed = staged(service, tmp_path)
    code = error_code(service.receive, receive_request("op-1", packed))
    assert code == "TELEPORT_STRICT_RESUME_FAILED"
    assert host.calls == HAPPY_CALLS + ["stop", "deactivate", "undo_landing"]
    assert host.running == set()
    assert host.landed == set()
    assert service.state("op-1") is None
    assert staging_entries(receiver) == []


def test_incomplete_stream(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    packed = packed_bundle(tmp_path)
    service.prepare(prepare_request("op-1"))
    feed(service, "op-1", packed, limit=1)
    assert len(chunks("op-1", packed)) > 1
    code = error_code(service.receive, receive_request("op-1", packed))
    assert code == "TELEPORT_INCOMPLETE"
    assert host.calls == []
    _assert_forgotten(service, host, receiver, "op-1", packed)


def test_receive_before_any_chunk_is_incomplete(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    packed = staged(service, tmp_path, "op-0")
    service.prepare(prepare_request("op-1"))
    code = error_code(service.receive, receive_request("op-1", packed))
    assert code == "TELEPORT_INCOMPLETE"
    assert host.calls == []
    assert service.state("op-1") is None
    assert staging_entries(receiver) == ["op-0"]


def _tampered(tmp_path: Path) -> Packed:
    bundle = make_bundle(tmp_path, "tampered")
    victim = bundle / "payload" / "workspace" / "B.txt"
    victim.write_bytes(b"UPPER\n")
    packed = stream().pack_bundle(bundle, tmp_path / "tampered.tar")
    return Packed(bundle, packed.path, packed.total, packed.sha256, manifest_digest(bundle))


def _wrong_manifest(tmp_path: Path, packed: Packed) -> Packed:
    return Packed(packed.bundle, packed.path, packed.total, packed.sha256, "cd" * 32)


DIGEST_CASES = ["stream-digest", "not-a-tar", "tampered-file", "manifest-digest"]


@pytest.mark.parametrize("case", DIGEST_CASES)
def test_digest_failures_before_landing(tmp_path: Path, case: str) -> None:
    service, host, receiver = make_service(tmp_path)
    service.prepare(prepare_request("op-1"))
    if case == "not-a-tar":
        packed = raw_stream(tmp_path, b"definitely not a tar stream " * 400)
    elif case == "tampered-file":
        packed = _tampered(tmp_path)
    else:
        packed = packed_bundle(tmp_path)
    feed(service, "op-1", packed)
    request = receive_request("op-1", packed)
    if case == "stream-digest":
        request = receive_request("op-1", packed, stream_sha256="ee" * 32)
    if case == "manifest-digest":
        request = receive_request("op-1", _wrong_manifest(tmp_path, packed))
    assert error_code(service.receive, request) == "TELEPORT_DIGEST_MISMATCH"
    assert host.calls == []
    _assert_forgotten(service, host, receiver, "op-1", packed)


def test_routines_not_announced_by_prepare_are_rejected(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    packed = staged(service, tmp_path)
    routines = (protocol().RoutinePayload(name="nightly", yaml="name: nightly\n", enabled=True),)
    code = error_code(service.receive, receive_request("op-1", packed, routines=routines))
    assert code == "TELEPORT_BAD_FRAME"
    assert host.calls == []
    _assert_forgotten(service, host, receiver, "op-1", packed)


def test_harness_must_match_prepare(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    packed = staged(service, tmp_path)
    code = error_code(service.receive, receive_request("op-1", packed, harness="codex"))
    assert code == "TELEPORT_BAD_FRAME"
    assert host.calls == []
    _assert_forgotten(service, host, receiver, "op-1", packed)


def test_unknown_operation(tmp_path: Path) -> None:
    service, host, _receiver = make_service(tmp_path)
    packed = staged(service, tmp_path, "op-known")
    code = error_code(service.receive, receive_request("op-unknown", packed))
    assert code == "TELEPORT_UNKNOWN_OPERATION"
    assert host.calls == []
    assert service.state("op-known") == "staging"

