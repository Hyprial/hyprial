# Decisions (package F10b, owner ruling on the F10 review, 2026-11-26):
# - The post-join body wait is best effort: a body still pending when the wait
#   ends is logged and the join goes on, so a CLI ``org join`` or ``org.join``
#   keeps the baseline ``8a38c910a8`` result (review S2a, S2b, S7) and only
#   waits a few seconds longer.  The outcome assertions (success, device row,
#   ``org.list``) hold on the baseline; the log and ``await_text`` assertions
#   are new and fail there.  All are RED on F10 ``694edc645e``.
# - Closing and a detached sync still end the wait with ``unavailable``; the
#   best-effort change must not swallow them (superseded by F10c below).
# - Package F10c (owner ruling on the F10b review, 2026-11-26): the wait changes
#   no outcome.  Closing, a detached sync and a removal from the ACL space end
#   the wait at once with an ``org.join.body-wait-ended`` log and the next
#   steps give the baseline result.  Wire tests go through ``METHOD_REGISTRY``
#   and a real ``ActorHostRegistry`` so the closed error schemas are checked;
#   raising had turned all six ``org.join`` / ``org.execute`` cases into
#   ``DAEMON_ERROR`` (F10b review).  A close is injected on a thread and the
#   join is held at the wait's end until it finishes, as in the baseline where
#   the close completes before the next step.  They are RED on F10b
#   ``98738f5b8c`` and on the baseline, which has no wait to hold the call in;
#   the baseline outcomes they assert were measured with the F10c probe.
# - Two branches the F10 review found unpinned (X2, X6) are asserted on the
#   timeouts handed to ``await_text``, which does not depend on machine load.
# - Fixtures and node helpers come from ``test_join_late_bodies``.
"""Best-effort post-join body wait: joins succeed as on the baseline (F10b)."""

from __future__ import annotations

from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.org.network import binding as org_binding
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.org.network.tailnet.budget import JoinBudget
from hyprial.identity import ORG_META_DOC
from hyprial.kernel import DaemonRequestError
from tests.daemon.org_membership.invites.test_join_late_bodies import (
    INVITEE,
    ORG,
    InviteeNode,
    LogRecorder,
    OwnerOrg,
    SyncGate,
    _call_held_in_body_wait,
    _holder_leaves_on_body_query,
    _spy_await_text,
)
from tests.daemon.orgfs.contracts.support.network import FakeNetwork


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


@pytest.fixture
def owner_org(tmp_path: Path) -> Any:
    org = OwnerOrg(tmp_path, FakeNetwork())
    try:
        yield org
    finally:
        org.close()


def _join(invitee: InviteeNode, entry: str) -> dict[str, Any]:
    if entry == "cli":
        return invitee.service.join(invitee.owner.link, claims=invitee.claims)
    return invitee.invoke("org.join", {"link": invitee.owner.link})


@pytest.mark.parametrize("entry", ["cli", "org.join"])
def test_join_succeeds_when_holder_leaves_after_join(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Review S2a/S2b: the holder drops before the ACL body arrives; the join still succeeds."""

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 0.4, raising=False)
    left = _holder_leaves_on_body_query(owner_org.network, "node-owner", owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org)
    log = LogRecorder()
    invitee.service._log = log
    try:
        started = time.monotonic()
        result = _join(invitee, entry)
        elapsed = time.monotonic() - started
        assert left.is_set()
        assert [step["kind"] for step in result["steps"]] == [
            "connect-inviter", "join-org", "register-device", "connect-members",
        ]
        assert elapsed < 3.0
        assert invitee.own_device_rows() == [invitee.node]
        listed = {row["org"]: row for row in invitee.service.list()["orgs"]}
        assert listed[ORG]["state"] == "active"
        pending = log.named("org.join.body-pending")
        assert [(row["org"], row["spaceId"], row["doc"]) for row in pending] == [
            (ORG, owner_org.acl_id, ACL_POLICY_DOC)
        ]
        assert 0.3 <= pending[0]["waitedSeconds"] < 3.0
    finally:
        invitee.close()


def test_cli_join_with_zero_wait_limit_skips_the_wait(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review S7: a join that used up its wait headroom skips the wait, reads nothing, succeeds."""

    original = JoinBudget.__init__

    def started_long_ago(self: JoinBudget, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.started -= org_binding.JOIN_ACTOR_HEADROOM_SECONDS + 1.0

    monkeypatch.setattr(JoinBudget, "__init__", started_long_ago)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    releaser = acl_gate.release_after(0.01)
    log = LogRecorder()
    invitee.service._log = log
    try:
        text_calls = _spy_await_text(invitee.runtime)
        started = time.monotonic()
        result = invitee.service.join(owner_org.link, claims=invitee.claims)
        assert time.monotonic() - started < 3.0
        assert result["org"] == ORG
        assert text_calls == []
        assert invitee.own_device_rows() == [invitee.node]
        assert [row["reason"] for row in log.named("org.join.body-wait-skipped")] == [
            "no-wait-budget"
        ]
    finally:
        acl_gate.open()
        releaser.join(timeout=2.0)
        invitee.close()


def _joined_with_held_bodies(
    tmp_path: Path, owner_org: OwnerOrg, gates: tuple[SyncGate, ...], **node: Any
) -> InviteeNode:
    invitee = InviteeNode(tmp_path, owner_org, gates=gates, **node)
    invitee.runtime.facade.join(owner_org.directory_id)
    invitee.runtime.facade.join(owner_org.acl_id)
    for gate in gates:
        assert gate.reached.wait(2.0)
    return invitee


def test_both_bodies_share_one_deadline(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review X2: two pending bodies wait one limit in total, not one each."""

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 0.3, raising=False)
    gates = (SyncGate(owner_org.directory_id), SyncGate(owner_org.acl_id))
    invitee = _joined_with_held_bodies(tmp_path, owner_org, gates)
    try:
        text_calls = _spy_await_text(invitee.runtime)
        log = LogRecorder()
        invitee.service._log = log
        org_binding.await_org_join_bodies(
            invitee.service, owner_org.directory_id, owner_org.acl_id, org=ORG
        )
        assert [space for space, _timeout in text_calls] == [owner_org.directory_id, owner_org.acl_id]
        assert sum(timeout for _space, timeout in text_calls) <= 0.3
        assert [row["doc"] for row in log.named("org.join.body-pending")] == [
            ORG_META_DOC, ACL_POLICY_DOC,
        ]
    finally:
        for gate in gates:
            gate.open()
        invitee.close()


def test_body_wait_is_capped_by_join_timeout(tmp_path: Path, owner_org: OwnerOrg) -> None:
    """Review X6: a service join timeout below the wait limit caps the wait."""

    acl_gate = SyncGate(owner_org.acl_id)
    invitee = _joined_with_held_bodies(tmp_path, owner_org, (acl_gate,), join_timeout_seconds=0.3)
    try:
        text_calls = _spy_await_text(invitee.runtime)
        org_binding.await_org_join_bodies(
            invitee.service, owner_org.directory_id, owner_org.acl_id, org=ORG
        )
        assert len(text_calls) == 2
        assert max(timeout for _space, timeout in text_calls) <= 0.3
    finally:
        acl_gate.open()
        invitee.close()


EARLY_EXITS = [
    ("close", "unavailable", "orgfs-closing"),
    ("detach", "unavailable", "sync-detached"),
    ("removed", "not-a-member", None),
]


class EventLog(LogRecorder):
    """Hold the join where the wait ends until an injected close has finished.

    The baseline has no wait; there the close finishes before the next step
    runs, and holding here gives the steps after the wait the same state.
    """

    def __init__(self) -> None:
        super().__init__()
        self.event_done = threading.Event()
        self.event_done.set()

    def __call__(self, level: str, area: str, event: str, **fields: Any) -> None:
        super().__call__(level, area, event, **fields)
        if event == "org.join.body-wait-ended":
            assert self.event_done.wait(10.0)


def _inject(event: str, invitee: InviteeNode, owner_org: OwnerOrg, log: EventLog) -> None:
    if event == "close":
        log.event_done.clear()

        def close() -> None:
            try:
                invitee.runtime.close()
            finally:
                log.event_done.set()

        threading.Thread(target=close, daemon=True).start()
    elif event == "detach":
        invitee.runtime.facade.detach_sync(owner_org.acl_id)
    else:
        owner_org.runtime.facade.remove_member(owner_org.acl_id, f"user:{INVITEE}")


def _ended_during_wait(
    call: Any, event: str, invitee: InviteeNode, owner_org: OwnerOrg, gate: SyncGate
) -> tuple[dict[str, Any], EventLog, float]:
    log = EventLog()
    invitee.service._log = log
    thread, box = _call_held_in_body_wait(call, invitee.runtime, (gate,))
    started = time.monotonic()
    _inject(event, invitee, owner_org, log)
    thread.join(timeout=10.0)
    assert box.get("done"), "the call did not return after the wait ended"
    assert log.event_done.wait(10.0)
    return box, log, time.monotonic() - started


@pytest.mark.parametrize(("event", "code", "reason"), EARLY_EXITS)
def test_wait_ends_quietly_on_early_exits(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch,
    event: str, code: str, reason: str | None,
) -> None:
    """F10c: closing, a detached sync or a removal ends the wait at once, logged, not raised."""

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 20.0, raising=False)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = _joined_with_held_bodies(tmp_path, owner_org, (acl_gate,))
    try:
        box, log, elapsed = _ended_during_wait(
            lambda: org_binding.await_org_join_bodies(
                invitee.service, owner_org.directory_id, owner_org.acl_id, org=ORG
            ),
            event, invitee, owner_org, acl_gate,
        )
        assert "error" not in box, box.get("error")
        assert elapsed < 5.0
        ended = log.named("org.join.body-wait-ended")
        assert [(row["org"], row["spaceId"], row["doc"], row["code"], row["reason"]) for row in ended] == [
            (ORG, owner_org.acl_id, ACL_POLICY_DOC, code, reason)
        ]
        assert not {"node", "path"} & set(ended[0])
        assert log.named("org.join.body-pending") == []
    finally:
        acl_gate.open()
        invitee.close()


@pytest.mark.parametrize("method", ["org.join", "org.execute"])
@pytest.mark.parametrize(("event", "code", "reason"), EARLY_EXITS)
def test_join_rpc_keeps_baseline_outcome_when_wait_ends_early(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch,
    method: str, event: str, code: str, reason: str | None,
) -> None:
    """F10c: through ``METHOD_REGISTRY`` the outcome is the baseline's, never ``DAEMON_ERROR``.

    Baseline ``8a38c910a8`` with the event right after ``join-org`` (F10c
    ``probe_ipc_exit.py``): close gives ``unavailable`` with empty data;
    detach and removal succeed with the device row registered.
    """

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 20.0, raising=False)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    try:
        box, log, elapsed = _ended_during_wait(
            lambda: invitee.invoke(method, {"link": owner_org.link}),
            event, invitee, owner_org, acl_gate,
        )
        assert elapsed < 5.0
        assert [(row["code"], row["reason"]) for row in log.named("org.join.body-wait-ended")] == [
            (code, reason)
        ]
        if event == "close":
            error = box.get("error")
            assert isinstance(error, DaemonRequestError), box
            assert error.code == "unavailable"
            assert not error.data
            assert invitee.own_device_rows() == []
        else:
            assert "error" not in box, box.get("error")
            assert [step["kind"] for step in box["result"]["steps"]] == [
                "connect-inviter", "join-org", "register-device", "connect-members",
            ]
            assert invitee.own_device_rows() == [invitee.node]
    finally:
        acl_gate.open()
        invitee.close()


@pytest.mark.parametrize(("event", "code", "reason"), EARLY_EXITS)
def test_accept_rpc_reports_declared_code_when_wait_ends_early(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch,
    event: str, code: str, reason: str | None,
) -> None:
    """F10c: accept reports the declared ``no-holder-online`` from its read-back.

    Same as baseline ``8a38c910a8`` with the ACL body held at the event (F10c
    probe, ``base-gated``).  With the body already there the baseline gives
    ``unavailable``/``register-device`` on close and success on detach; F10c
    measures the same in that timing, where the wait has already returned.
    """

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 20.0, raising=False)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = InviteeNode(tmp_path, owner_org, gates=(acl_gate,))
    try:
        box, log, elapsed = _ended_during_wait(invitee.accept, event, invitee, owner_org, acl_gate)
        assert elapsed < 5.0
        assert [(row["code"], row["reason"]) for row in log.named("org.join.body-wait-ended")] == [
            (code, reason)
        ]
        error = box.get("error")
        assert isinstance(error, DaemonRequestError), box
        assert error.code == "no-holder-online"
        assert error.data is not None
        assert (error.data["missingStep"], error.data["status"]) == ("acl-applied", "partial")
        assert invitee.own_device_rows() == []
    finally:
        acl_gate.open()
        invitee.close()


def test_a_service_without_orgfs_has_nothing_to_wait_for() -> None:
    """An executor with no OrgFS (contract producer examples) skips the wait."""

    org_binding.await_org_join_bodies(SimpleNamespace(), "space-dir", "space-acl", org=ORG)
