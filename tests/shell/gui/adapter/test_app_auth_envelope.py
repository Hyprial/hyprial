"""N-A2 consumption-side tests (v0.2): envelope, digest basis, matrix, telemetry.

The envelope's MAC pre-image and field order are pinned against the values the
repository's fake sidecar answers for ``derive`` (``tests/fixtures/network/
fake_forwarding_sidecar.py``, wire shape per #1293: Role+Peer in, base64(32B)
key out); the deterministic fixture key stands in for the real derivation,
which this slice never re-implements.  v0.2 pins: the version, host node id
and device generation are inside the signed pre-image and checked before any
side effect; the digest covers the whole body without ``auth``; ``author``
lives in the body; the nonce burns only after the MAC verifies; and every
refusal carries its reference reason.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from hyprial.shell.impl.gui.adapter.chat import AppChatAdapter
from hyprial.shell.impl.gui.adapter.envelope import (
    AUTHOR_MISMATCH,
    DERIVE_UNAVAILABLE,
    DERIVE_UNSUPPORTED,
    DEVICE_GENERATION_STALE,
    DEVICE_REVOKED,
    DEVICE_UNKNOWN,
    ENVELOPE_INVALID,
    ENVELOPE_MISSING,
    ENVELOPE_VERSION,
    ENVELOPE_VERSION_UNSUPPORTED,
    NONCE_REPLAYED,
    NONCE_WINDOW_FULL,
    REASON_AUTHOR_DEVICE_MISMATCH,
    REASON_DERIVE_UNAVAILABLE,
    REASON_DEVICE_AUTHENTICATION_REQUIRED,
    REASON_DEVICE_REVOKED,
    REASON_MAC_MISMATCH,
    REASON_MAC_MISSING,
    REASON_MALFORMED_AUTH,
    REASON_REPLAYED_REQUEST,
    REASON_SIDECAR_DERIVE_UNSUPPORTED,
    REASON_STALE_DEVICE_GENERATION,
    REASON_STALE_REQUEST,
    REASON_UNKNOWN_DEVICE,
    REASON_UNSUPPORTED_ENVELOPE,
    REASON_WRONG_TARGET_NODE,
    TARGET_NODE_MISMATCH,
    TIMESTAMP_SKEW,
    AuthGate,
    DeviceRow,
    DeriveUnsupported,
    mac_text,
    request_digest,
)
from hyprial.shell.impl.gui.adapter.host import (
    adapter_from_config,
    handle_request,
    serve_tcp,
)
from tests.shell.gui.adapter.test_app_adapter_host import (
    CONFIG,
    FakeDaemon,
    TcpFrameClient,
)

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/network/fake_forwarding_sidecar.py"
)

OWNER = "agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"
PEER = "fixture-client-public"
NODE = "node:dev000000001.local"


class FixtureDerive:
    """The repository fake sidecar, spoken to in its own stdio protocol."""

    def __init__(self, tmp_path: Path) -> None:
        assert FIXTURE.is_file(), f"the fake sidecar fixture is missing: {FIXTURE}"
        receipt = tmp_path / "receipt.jsonl"
        self.process = subprocess.Popen(
            [sys.executable, str(FIXTURE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={"FAKE_FORWARD_RECEIPT": str(receipt)},
        )
        hello = self._read_event()
        self.capabilities = hello["capabilities"]
        self.calls: list[tuple[str, str]] = []

    def _read_event(self) -> dict:
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError("the fake sidecar closed its stream")
        return json.loads(line)

    def __call__(self, role: str, peer: str) -> bytes:
        """Callable spelling, so the fixture doubles as the derive seam."""

        return self.derive(role, peer)

    def derive(self, role: str, peer: str) -> bytes:
        self.calls.append((role, peer))
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(
            json.dumps({"v": 3, "op": "derive", "role": role, "peer": peer}) + "\n"
        )
        self.process.stdin.flush()
        event = self._read_event()
        self.last_event = event
        assert event["event"] == "derived", event
        key = base64.b64decode(event["key"])
        assert len(key) == 32, "#1293: the derived key is 32 bytes, base64 on the wire"
        return key

    def close(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.write(json.dumps({"v": 3, "op": "down"}) + "\n")
            self.process.stdin.flush()
            self.process.wait(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture()
def fixture_derive(tmp_path: Path):
    derive = FixtureDerive(tmp_path)
    yield derive
    derive.close()


def make_gate(
    derive,
    *,
    clock=lambda: 1_000_000.0,
    devices=None,
    window_seconds: float = 120.0,
    nonce_capacity: int = 4096,
) -> AuthGate:
    table = {"dev-1": DeviceRow(owner=OWNER, client_public=PEER)} if devices is None else devices
    return AuthGate(
        derive=derive,
        device_row=table.get,
        target_node_id=NODE,
        clock=clock,
        window_seconds=window_seconds,
        nonce_capacity=nonce_capacity,
    )


def signed_frame(
    derive,
    *,
    operation: str = "capabilities.get",
    params: dict | None = None,
    ts: int = 1_000_000,
    nonce: str = "nonce-1",
    device_id: str = "dev-1",
    generation: int = 1,
    target_node_id: str = NODE,
    version: int = ENVELOPE_VERSION,
    author: str | None = None,
    mac: str | None = None,
) -> dict:
    body: dict = {"operation": operation, "params": dict(params or {})}
    if author is not None:
        body["author"] = author
    body_params = body["params"]
    session_id = body_params.get("sessionId")
    space = session_id if isinstance(session_id, str) and session_id else ""
    digest = request_digest(body)
    key = derive("server", PEER)
    expected = mac or hmac.new(
        key,
        mac_text(
            version,
            target_node_id,
            generation,
            ts,
            nonce,
            space,
            operation,
            digest,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    frame = dict(body)
    frame["auth"] = {
        "version": version,
        "deviceId": device_id,
        "deviceGeneration": generation,
        "targetNodeId": target_node_id,
        "ts": ts,
        "nonce": nonce,
        "mac": expected,
    }
    return frame


def make_adapter(*answers: object) -> AppChatAdapter:
    return adapter_from_config(CONFIG, FakeDaemon(*answers))


# --- envelope: field order, digest basis, accepted path ---------------------


def test_mac_preimage_is_the_frozen_pipe_joined_order(fixture_derive) -> None:
    assert "derive" in fixture_derive.capabilities
    key_first = fixture_derive.derive("server", PEER)
    key_again = fixture_derive.derive("server", PEER)
    assert key_first == key_again, "the fixture answers deterministically per (role, peer)"
    assert len(key_first) == 32
    assert key_first != fixture_derive.derive("client", PEER)

    digest = request_digest({"operation": "sessions.send", "params": {"sessionId": "s-1"}})
    assert mac_text(2, NODE, 3, 7, "n", "s-1", "sessions.send", digest) == (
        f"2|{NODE}|3|7|n|s-1|sessions.send|{digest}"
    )


def test_the_digest_covers_the_whole_body_without_auth(fixture_derive) -> None:
    base = {"operation": "sessions.history", "params": {"sessionId": "s-1"}}
    with_author = {**base, "author": OWNER}

    # The auth key itself is excluded; everything else participates.
    assert (
        request_digest({**base, "auth": {"deviceId": "dev-1"}}) == request_digest(base)
    )
    assert request_digest(with_author) != request_digest(base)
    assert request_digest({**base, "params": {"sessionId": "s-2"}}) != request_digest(base)
    assert (
        request_digest({"operation": "sessions.events", "params": {"sessionId": "s-1"}})
        != request_digest(base)
    )


def test_a_verified_envelope_reaches_the_adapter_unchanged(fixture_derive) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive)

    answer = handle_request(
        adapter,
        signed_frame(fixture_derive, operation="capabilities.get"),
        gate=gate,
    )

    assert answer["ok"] is True
    assert answer["result"]["operations"]["sessions.send"] == {"supported": True}


def test_author_both_directions(fixture_derive) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive)

    # Absent author: the device row's owner is the identity.
    absent = handle_request(
        adapter, signed_frame(fixture_derive, nonce="n-a1"), gate=gate
    )
    assert absent["ok"] is True

    # author == owner: allowed.
    matching = handle_request(
        adapter,
        signed_frame(fixture_derive, nonce="n-a2", author=OWNER),
        gate=gate,
    )
    assert matching["ok"] is True

    # author != owner: refused with the reference reason, before any MAC use.
    mismatched = handle_request(
        adapter,
        signed_frame(fixture_derive, nonce="n-a3", author="agent:someone-else"),
        gate=gate,
    )
    assert mismatched["error"]["code"] == AUTHOR_MISMATCH
    assert mismatched["error"]["details"]["reason"] == REASON_AUTHOR_DEVICE_MISMATCH


def test_envelope_smuggled_identity_is_still_the_adapters_business(fixture_derive) -> None:
    # The envelope proves the device; the adapter still refuses identity
    # fields inside params -- two independent layers.
    adapter = make_adapter()
    gate = make_gate(fixture_derive)
    frame = signed_frame(
        fixture_derive,
        operation="sessions.send",
        params={
            "sessionId": "s-1",
            "request": {"text": "hi", "idempotencyKey": "k-1", "from": "someone-else"},
        },
    )

    answer = handle_request(adapter, frame, gate=gate)

    assert answer["ok"] is False
    assert answer["error"]["code"] == "identity-rejected"


# --- envelope: every rejection class, with its reference reason -------------


@pytest.mark.parametrize(
    "mutate,code,reason",
    [
        (lambda f: f.pop("auth"), ENVELOPE_MISSING, REASON_DEVICE_AUTHENTICATION_REQUIRED),
        (lambda f: f["auth"].pop("version"), ENVELOPE_MISSING, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].pop("targetNodeId"), ENVELOPE_MISSING, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].pop("deviceGeneration"), ENVELOPE_MISSING, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].pop("nonce"), ENVELOPE_MISSING, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].pop("mac"), ENVELOPE_INVALID, REASON_MAC_MISSING),
        (lambda f: f["auth"].update({"version": 1}), ENVELOPE_VERSION_UNSUPPORTED, REASON_UNSUPPORTED_ENVELOPE),
        (lambda f: f["auth"].update({"version": 3}), ENVELOPE_VERSION_UNSUPPORTED, REASON_UNSUPPORTED_ENVELOPE),
        (lambda f: f["auth"].update({"version": True}), ENVELOPE_VERSION_UNSUPPORTED, REASON_UNSUPPORTED_ENVELOPE),
        (lambda f: f["auth"].update({"targetNodeId": "node:somewhere-else"}), TARGET_NODE_MISMATCH, REASON_WRONG_TARGET_NODE),
        (lambda f: f["auth"].update({"targetNodeId": ""}), ENVELOPE_INVALID, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].update({"deviceGeneration": 2}), DEVICE_GENERATION_STALE, REASON_STALE_DEVICE_GENERATION),
        (lambda f: f["auth"].update({"deviceGeneration": 0}), ENVELOPE_INVALID, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].update({"extra": 1}), ENVELOPE_INVALID, REASON_MALFORMED_AUTH),
        (lambda f: f.update({"extra": 1}), ENVELOPE_INVALID, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].update({"ts": 1_000_000.5}), ENVELOPE_INVALID, REASON_MALFORMED_AUTH),
        (lambda f: f.update({"author": 7}), ENVELOPE_INVALID, REASON_MALFORMED_AUTH),
        (lambda f: f["auth"].update({"mac": "00" * 32}), ENVELOPE_INVALID, REASON_MAC_MISMATCH),
        (lambda f: f["auth"].update({"deviceId": "dev-unknown"}), DEVICE_UNKNOWN, REASON_UNKNOWN_DEVICE),
        (lambda f: f["auth"].update({"ts": 1_000_121}), TIMESTAMP_SKEW, REASON_STALE_REQUEST),
        (lambda f: f["auth"].update({"ts": 999_879}), TIMESTAMP_SKEW, REASON_STALE_REQUEST),
    ],
)
def test_envelope_rejections_are_definitive_with_reference_reasons(
    fixture_derive, mutate, code, reason
) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive)
    frame = signed_frame(fixture_derive)
    mutate(frame)

    answer = handle_request(adapter, frame, gate=gate)

    assert answer["ok"] is False, answer
    assert answer["error"]["code"] == code, answer
    assert answer["error"]["definitive"] is True
    assert answer["error"]["details"]["reason"] == reason


def test_a_revoked_device_is_refused_before_any_business_dispatch(
    fixture_derive,
) -> None:
    """The device-row source is authoritative, so revocation is not cached.

    A revoked tombstone refuses the next request before MAC work, authorization
    or dispatch; a re-paired device with a newer generation refuses the old
    generation even though that generation's key still verifies.
    """

    adapter = make_adapter()
    live = {"dev-1": DeviceRow(owner=OWNER, client_public=PEER)}
    gate = make_gate(fixture_derive, devices=live)
    assert (
        handle_request(
            adapter, signed_frame(fixture_derive, nonce="before-revoke"), gate=gate
        )["ok"]
        is True
    )

    live["dev-1"] = DeviceRow(
        owner=OWNER, client_public=PEER, key_generation=1, status="revoked"
    )
    refused = handle_request(
        adapter, signed_frame(fixture_derive, nonce="after-revoke"), gate=gate
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == DEVICE_REVOKED
    assert refused["error"]["details"]["reason"] == REASON_DEVICE_REVOKED

    live["dev-1"] = DeviceRow(
        owner=OWNER, client_public=PEER, key_generation=2, status="active"
    )
    stale = handle_request(
        adapter, signed_frame(fixture_derive, nonce="old-generation"), gate=gate
    )
    assert stale["ok"] is False
    assert stale["error"]["code"] == DEVICE_GENERATION_STALE
    assert stale["error"]["details"]["reason"] == REASON_STALE_DEVICE_GENERATION
    assert handle_request(
        adapter,
        signed_frame(fixture_derive, nonce="new-generation", generation=2),
        gate=gate,
    )["ok"] is True


def test_tampered_params_break_the_mac(fixture_derive) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive)
    frame = signed_frame(
        fixture_derive,
        operation="sessions.history",
        params={"sessionId": "s-1", "args": {"limit": 5}},
        author=OWNER,
    )
    frame["params"]["args"]["limit"] = 6  # unsigned change after signing

    answer = handle_request(adapter, frame, gate=gate)

    assert answer["error"]["code"] == ENVELOPE_INVALID
    assert answer["error"]["details"]["reason"] == REASON_MAC_MISMATCH


def test_the_nonce_burns_only_after_the_mac_verifies(fixture_derive) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive)
    nonce = "burn-order"

    # A guessed MAC must not poison the window for this nonce...
    guess = signed_frame(fixture_derive, nonce=nonce)
    guess["auth"]["mac"] = "ab" * 32
    refused = handle_request(adapter, guess, gate=gate)
    assert refused["error"]["details"]["reason"] == REASON_MAC_MISMATCH

    # ...so the device's own signed request with the same nonce still passes.
    honest = signed_frame(fixture_derive, nonce=nonce)
    assert handle_request(adapter, honest, gate=gate)["ok"] is True

    # And only now is it burned: a second honest attempt replays.
    again = handle_request(adapter, honest, gate=gate)
    assert again["error"]["code"] == NONCE_REPLAYED
    assert again["error"]["details"]["reason"] == REASON_REPLAYED_REQUEST


def test_nonce_replay_is_refused_until_the_window_expires(fixture_derive) -> None:
    adapter = make_adapter()
    now = [1_000_000.0]
    gate = make_gate(fixture_derive, clock=lambda: now[0])
    frame = signed_frame(fixture_derive, nonce="replay-me")

    assert handle_request(adapter, frame, gate=gate)["ok"] is True
    replay = handle_request(adapter, frame, gate=gate)
    assert replay["error"]["code"] == NONCE_REPLAYED

    # A later window forgets the nonce: same nonce, fresh ts, accepted again.
    now[0] = 1_000_121.0
    later = signed_frame(fixture_derive, nonce="replay-me", ts=1_000_121)
    assert handle_request(adapter, later, gate=gate)["ok"] is True


def test_the_nonce_window_refuses_rather_than_evicting_live_evidence(fixture_derive) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive, nonce_capacity=2)
    for nonce in ("n-1", "n-2"):
        assert handle_request(adapter, signed_frame(fixture_derive, nonce=nonce), gate=gate)["ok"] is True

    answer = handle_request(adapter, signed_frame(fixture_derive, nonce="n-3"), gate=gate)

    assert answer["error"]["code"] == NONCE_WINDOW_FULL
    # The capacity is local hardening: its refusal is deliberately NOT one of
    # the frozen reference reasons.
    assert answer["error"]["details"]["reason"] == "nonce-window-full"


def test_derive_unsupported_and_unavailable_never_downgrade_to_unsigned() -> None:
    adapter = make_adapter()

    def unsupported(_role: str, _peer: str) -> bytes:
        raise DeriveUnsupported("SIDECAR_DERIVE_UNSUPPORTED")

    def broken(_role: str, _peer: str) -> bytes:
        raise RuntimeError("the sidecar socket is gone")

    frame = signed_frame(lambda role, peer: b"\x01" * 32, operation="capabilities.get")
    unsupported_answer = handle_request(adapter, frame, gate=make_gate(unsupported))
    assert unsupported_answer["error"]["code"] == DERIVE_UNSUPPORTED
    assert (
        unsupported_answer["error"]["details"]["reason"]
        == REASON_SIDECAR_DERIVE_UNSUPPORTED
    )
    assert "SIDECAR_DERIVE_UNSUPPORTED" in unsupported_answer["error"]["message"]

    unavailable_answer = handle_request(adapter, frame, gate=make_gate(broken))
    assert unavailable_answer["error"]["code"] == DERIVE_UNAVAILABLE
    assert unavailable_answer["error"]["details"]["reason"] == REASON_DERIVE_UNAVAILABLE


def test_refusals_leave_one_telemetry_line_with_the_reason(
    fixture_derive, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive)
    frame = signed_frame(fixture_derive)
    frame["auth"]["mac"] = "00" * 32

    handle_request(adapter, frame, gate=gate)

    captured = capsys.readouterr()
    assert "reason=mac-mismatch" in captured.err
    assert "code=auth-envelope-invalid" in captured.err


# --- authorization matrix ---------------------------------------------------


def test_matrix_allows_the_owners_session_and_denies_everything_else(fixture_derive) -> None:
    adapter = make_adapter(
        {"ok": True, "conversationId": "conv-1", "entries": [], "nextCursor": None},
    )
    gate = make_gate(fixture_derive)

    allowed = handle_request(
        adapter,
        signed_frame(fixture_derive, operation="sessions.history", params={"sessionId": "s-1"}),
        gate=gate,
    )
    assert allowed == {"ok": True, "result": {"events": [], "nextCursor": None}}

    # A session the composition root never bound.
    unknown = handle_request(
        adapter,
        signed_frame(
            fixture_derive,
            operation="sessions.history",
            params={"sessionId": "s-9"},
            nonce="n-2",
        ),
        gate=gate,
    )
    assert unknown["error"]["code"] == "auth-not-authorized"

    # A session-scoped operation with no session named.
    sessionless = handle_request(
        adapter,
        signed_frame(fixture_derive, operation="sessions.send", nonce="n-3"),
        gate=gate,
    )
    assert sessionless["error"]["code"] == "auth-not-authorized"

    # A sessionless operation that smuggles a session id.
    smuggled = handle_request(
        adapter,
        signed_frame(
            fixture_derive,
            operation="capabilities.get",
            params={"sessionId": "s-1"},
            nonce="n-4",
        ),
        gate=gate,
    )
    assert smuggled["error"]["code"] == "auth-not-authorized"

    # An operation with no matrix row.
    norow = handle_request(
        adapter,
        signed_frame(fixture_derive, operation="sessions.delete", nonce="n-5"),
        gate=gate,
    )
    assert norow["error"]["code"] == "auth-not-authorized"

    # The network directory and session creation are local-desktop operations
    # in this slice: they have no matrix row, so a remote device is denied
    # until explicit per-session sharing is designed.  The gate runs before
    # the adapter, so neither call can reach the daemon.
    for nonce, operation in (("n-6", "targets.list"), ("n-7", "sessions.create")):
        denied = handle_request(
            adapter,
            signed_frame(fixture_derive, operation=operation, nonce=nonce),
            gate=gate,
        )
        assert denied["error"]["code"] == "auth-not-authorized"


def test_matrix_denies_an_owner_who_is_not_a_session_target(fixture_derive) -> None:
    # The device row is valid and the MAC verifies, but the binding's targets
    # are a different actor: the owner cannot reach this session.
    other_owner = "agent:chloe.martin@example.com:dev000000001.local:other"
    config = {
        "sender": OWNER,
        "sessions": {
            "s-1": {"targets": [other_owner], "conversationId": "conv-1"},
        },
    }
    adapter = adapter_from_config(config, FakeDaemon())
    gate = make_gate(fixture_derive)

    answer = handle_request(
        adapter,
        signed_frame(fixture_derive, operation="sessions.history", params={"sessionId": "s-1"}),
        gate=gate,
    )

    assert answer["error"]["code"] == "auth-not-authorized"
    assert answer["error"]["details"]["reason"] == (
        "the author is not one of the session's bound targets"
    )


# --- the remote facade applies the gate; the desktop path does not ----------


def test_tcp_facade_gate_end_to_end(fixture_derive) -> None:
    adapter = make_adapter()
    gate = make_gate(fixture_derive)
    bound: list[tuple[str, int]] = []
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_tcp,
        args=(0, adapter),
        kwargs={
            "ready": ready,
            "stop": stop,
            "on_bound": lambda host, port: bound.append((host, port)),
            "auth_gate": gate,
        },
    )
    thread.start()
    assert ready.wait(5.0)
    address, port = bound[0]
    try:
        with TcpFrameClient(address, port) as client:
            client.send(signed_frame(fixture_derive))
            assert client.read()["ok"] is True
            client.send({"operation": "capabilities.get", "params": {}})
            unsigned = client.read()
            assert unsigned["error"]["code"] == ENVELOPE_MISSING
            assert (
                unsigned["error"]["details"]["reason"]
                == REASON_DEVICE_AUTHENTICATION_REQUIRED
            )
            assert unsigned["error"]["definitive"] is True
    finally:
        stop.set()
        thread.join(timeout=3.0)


# --- unconfigured posture is exactly the old one ----------------------------


def test_without_a_gate_requests_take_the_old_path() -> None:
    adapter = make_adapter(
        {"ok": True, "operationId": "op-1", "conversationId": "conv-1",
         "deliveries": [{"target": "agent:t", "messageId": "m-1", "accepted": True, "queued": False}],
         "messageId": "m-1"},
    )

    # No envelope required, and none of the envelope vocabulary appears.
    assert handle_request(adapter, {"operation": "capabilities.get", "params": {}})["ok"] is True
    send = handle_request(
        adapter,
        {
            "operation": "sessions.send",
            "params": {"sessionId": "s-1", "request": {"text": "hi", "idempotencyKey": "k-1"}},
        },
    )
    assert send["ok"] is True

    # The identity smuggling refusal is unchanged too.
    smuggled = handle_request(
        adapter,
        {
            "operation": "sessions.send",
            "params": {
                "sessionId": "s-1",
                "request": {"text": "hi", "idempotencyKey": "k-1", "from": "elsewhere"},
            },
        },
    )
    assert smuggled["error"]["code"] == "identity-rejected"
