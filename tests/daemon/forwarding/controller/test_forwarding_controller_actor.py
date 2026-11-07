from __future__ import annotations

import threading

from hyprial.daemon.impl.forwarding import ForwardingControllerAuthority


def test_native_sidecar_io_has_one_typed_owner_and_immutable_status() -> None:
    calls: list[str] = []

    class Controller:
        pid = 123
        peers_reported = True

        def status(self):  # type: ignore[no-untyped-def]
            calls.append("status")
            return ("peer-a",), {}

        def map_peer(self, peer: str, address: str, slot: str) -> str:
            calls.append(f"map:{peer}:{address}:{slot}")
            return slot

        def unmap_peer(self, peer: str) -> None:
            calls.append(f"unmap:{peer}")

        def close(self) -> None:
            calls.append("close")

    owner = ForwardingControllerAuthority(Controller())
    assert owner.pid == 123
    assert owner.status() == (("peer-a",), {})
    assert owner.peers_reported is True
    assert owner.map_peer("peer-a", "tailcat://peer-a", "/s/slot-00") == "/s/slot-00"
    owner.unmap_peer("peer-a")
    owner.close()
    assert calls == [
        "status", "map:peer-a:tailcat://peer-a:/s/slot-00", "unmap:peer-a", "close",
    ]


def test_blocked_native_call_keeps_close_ordered_behind_it() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class Controller:
        def status(self):  # type: ignore[no-untyped-def]
            entered.set()
            assert release.wait(2)
            calls.append("status")
            return (), {}

        def close(self) -> None:
            calls.append("close")

    owner = ForwardingControllerAuthority(Controller())
    worker = threading.Thread(target=owner.status)
    worker.start()
    assert entered.wait(1)
    closed = threading.Event()
    closer = threading.Thread(target=lambda: (owner.close(), closed.set()))
    closer.start()
    assert not closed.wait(0.02)
    release.set()
    worker.join(2)
    closer.join(2)
    assert closed.is_set()
    assert calls == ["status", "close"]


def test_membership_calls_from_a_request_path_are_bounded_with_a_stable_code() -> None:
    """A stalled membership command must not hang an IPC reply (0.5.5.1 hotfix).

    The sidecar I/O owner is serial and gave every membership command a 35s
    allowance; from a CLI request path (`invite-create`, redeem, status) that
    is past the CLI's 15s roundtrip.  A caller-supplied bound returns a
    ForwardingSidecarError with the stable MEMBERSHIP_SIDECAR_TIMEOUT code.
    """
    import time

    import pytest

    from hyprial.daemon.impl.forwarding import ForwardingSidecarError
    from hyprial.kernel import ipc_errors

    entered = threading.Event()
    release = threading.Event()

    class Controller:
        def membership_request(self, op: str, params: dict, *, timeout=None) -> dict:  # type: ignore[type-arg]
            entered.set()
            assert release.wait(5)
            return {"op": op, **params}

        def close(self) -> None:
            pass

    owner = ForwardingControllerAuthority(Controller())
    started = time.monotonic()
    with pytest.raises(ForwardingSidecarError) as caught:
        owner.membership_request("invite-create", {"tailnetId": "t"}, timeout=0.05)
    assert time.monotonic() - started < 2.0
    assert caught.value.code == ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT
    assert entered.is_set()
    release.set()
    owner.close()


def test_membership_wait_budget_nests_inside_the_cli_roundtrip() -> None:
    """Request and background paths share operation budgets with explicit overrides."""
    from hyprial.daemon.impl.forwarding.policy import membership_wait_seconds
    from hyprial.kernel import MEMBERSHIP_BUDGETS

    assert membership_wait_seconds("invite-redeem", None) == 65
    assert membership_wait_seconds("invite-redeem", 60) == 60
    assert membership_wait_seconds("member-announce", None) == 35
    assert membership_wait_seconds("membership-status", None) == 10
    assert membership_wait_seconds("invite-redeem", 120) == 65
    assert MEMBERSHIP_BUDGETS["join-cli"].seconds > MEMBERSHIP_BUDGETS["join"].seconds
