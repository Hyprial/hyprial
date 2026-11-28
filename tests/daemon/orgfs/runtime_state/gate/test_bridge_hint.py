# Decisions (phase F1, node gate ruling, 2026-11-25):
# - ``TailnetDeviceOwners`` is the assembly's ``device_owner_hint``: active
#   tailnet members only, owner names normalised to ``user:`` principals, one
#   status per TTL, no answer when the sidecar fails or two tailnets disagree.
# - Package S (2026-11-25): there is one runtime, so the bridge always binds
#   the hint through ``bind_device_owner_hint``.  The runtime class is
#   replaced by a recorder so no daemon is started.
"""The bridge's tailnet-roster hint for the state runtime's node gate."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.application.messaging import orgfs_bridge
from hyprial.daemon.impl.application.messaging.orgfs_bridge import TailnetDeviceOwners


def _status(*members: dict[str, Any], ready: bool = True, tailnet: str = "t-1") -> dict[str, Any]:
    return {"tailnets": [{"tailnetId": tailnet, "memberReady": ready, "members": list(members)}]}


def _member(device: str, owner: str, state: str = "active") -> dict[str, Any]:
    return {"deviceId": device, "owner": owner, "state": state, "masterPublic": "m", "address": "a"}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_active_members_map_to_user_principals_and_revoked_devices_to_nothing() -> None:
    owners = TailnetDeviceOwners(lambda: _status(
        _member("node-owner", "owner@example"),
        _member("node-member", "user:member@example"),
        _member("node-gone", "gone@example", state="revoked"),
    ))

    assert owners("node-owner") == "user:owner@example"
    assert owners("node-member") == "user:member@example"
    assert owners("node-gone") is None
    assert owners("node-stranger") is None


def test_a_tailnet_that_is_not_member_ready_gives_no_principal() -> None:
    owners = TailnetDeviceOwners(lambda: _status(_member("node-owner", "owner@example"), ready=False))

    assert owners("node-owner") is None


def test_one_status_serves_the_ttl_then_the_roster_is_read_again() -> None:
    clock = _Clock()
    reads: list[int] = []
    roster = [_member("node-owner", "owner@example")]

    def status() -> dict[str, Any]:
        reads.append(1)
        return _status(*roster)

    owners = TailnetDeviceOwners(status, ttl=5.0, clock=clock)
    assert owners("node-owner") == "user:owner@example"
    roster.append(_member("node-member", "member@example"))
    assert owners("node-member") is None
    assert len(reads) == 1
    clock.now = 5.0
    assert owners("node-member") == "user:member@example"
    assert len(reads) == 2


def test_a_failing_sidecar_and_a_conflicting_roster_give_no_principal() -> None:
    events: list[str] = []

    def broken() -> dict[str, Any]:
        raise RuntimeError("sidecar down")

    failing = TailnetDeviceOwners(broken, log=lambda _level, event, **_f: events.append(event))
    assert failing("node-owner") is None
    conflicting = TailnetDeviceOwners(
        lambda: {"tailnets": [
            *_status(_member("node-x", "a@example"), tailnet="t-1")["tailnets"],
            *_status(_member("node-x", "b@example"), tailnet="t-2")["tailnets"],
        ]},
        log=lambda _level, event, **_f: events.append(event),
    )
    assert conflicting("node-x") is None
    assert events == ["orgfs.gate.roster-unavailable", "orgfs.gate.roster-conflict"]


class _StateLike:
    def __init__(self, state_dir: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.logger = kwargs.get("logger")
        self.hint: Any = None

    def bind_device_owner_hint(self, hint: Any) -> None:
        self.hint = hint


class _App(orgfs_bridge._OrgFsBridgeMixin):  # noqa: SLF001 - the bridge mixin under test
    def __init__(self, tmp_path: Any) -> None:
        self.state_dir = tmp_path / "state"
        self.hyprial_home = tmp_path / "home"
        self.node_id = "node-owner"
        self.owner = "owner@example"
        self._orgfs_runtime = None
        self._actor_hosts = SimpleNamespace(host_for=lambda _name: None)

    def _log(self, *_args: Any, **_fields: Any) -> None:
        return None


def test_the_bridge_binds_the_hint_on_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(orgfs_bridge, "OrgFsRuntime", _StateLike)
    runtime = _App(tmp_path)._require_orgfs_runtime()

    assert "device_owner_hint" not in runtime.kwargs
    assert isinstance(runtime.hint, TailnetDeviceOwners)
    assert runtime.hint("node-owner") is None
