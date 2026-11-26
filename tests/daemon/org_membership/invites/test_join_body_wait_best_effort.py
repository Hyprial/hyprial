# Decisions (package F10b, owner ruling on the F10 review, 2026-11-26):
# - The post-join body wait is best effort: a body still pending when the wait
#   ends is logged and the join goes on, so a CLI ``org join`` or ``org.join``
#   keeps the baseline ``8a38c910a8`` result (review S2a, S2b, S7) and only
#   waits a few seconds longer.  The outcome assertions (success, device row,
#   ``org.list``) hold on the baseline; the log and ``await_text`` assertions
#   are new and fail there.  All are RED on F10 ``694edc645e``.
# - Closing and a detached sync still end the wait with ``unavailable``; the
#   best-effort change must not swallow them.
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
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import ORG_META_DOC
from tests.daemon.org_membership.invites.test_join_late_bodies import (
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


@pytest.mark.parametrize(
    ("event", "reason"), [("close", "orgfs-closing"), ("detach", "sync-detached")]
)
def test_best_effort_wait_still_raises_early_exits(
    tmp_path: Path, owner_org: OwnerOrg, monkeypatch: pytest.MonkeyPatch, event: str, reason: str
) -> None:
    """Closing or a detached sync is not a late body: the wait raises ``unavailable``."""

    monkeypatch.setattr(org_binding, "JOIN_BODY_WAIT_SECONDS", 20.0, raising=False)
    acl_gate = SyncGate(owner_org.acl_id)
    invitee = _joined_with_held_bodies(tmp_path, owner_org, (acl_gate,))
    try:
        thread, box = _call_held_in_body_wait(
            lambda: org_binding.await_org_join_bodies(
                invitee.service, owner_org.directory_id, owner_org.acl_id, org=ORG
            ),
            invitee.runtime,
            (acl_gate,),
        )
        if event == "close":
            threading.Thread(target=invitee.runtime.close, daemon=True).start()
        else:
            invitee.runtime.facade.detach_sync(owner_org.acl_id)
        thread.join(timeout=5.0)
        assert box.get("done")
        error = box.get("error")
        assert isinstance(error, OrgFsError)
        assert (error.code, error.details.get("reason")) == ("unavailable", reason)
    finally:
        acl_gate.open()
        invitee.close()


def test_a_service_without_orgfs_has_nothing_to_wait_for() -> None:
    """An executor with no OrgFS (contract producer examples) skips the wait."""

    org_binding.await_org_join_bodies(SimpleNamespace(), "space-dir", "space-acl", org=ORG)
