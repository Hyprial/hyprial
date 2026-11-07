"""The declared link result schemas cover real service branches, not just empties."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.links.model import LinkEntry
from hyprial.daemon import HOLD_REASONS
from hyprial.daemon.impl.request_catalog import link as link_catalog
from hyprial.kernel import ipc_errors
from tests.support.links.org import (
    FakeOrgFs,
    establish,
    establish_many,
    make_service,
    write_reference,
)

A = "agent:o:n1:a"
B = "agent:o:n1:b"
C = "agent:o:n1:c"


class _App:
    owner = "o"
    node_id = "n1"

    def __init__(self, service: Any) -> None:
        self._link_service = service

    def _canonical_interactive_actor(self, actor: str) -> str:
        return actor

    def _authenticated_message_caller(
        self, params: dict[str, Any], *, method: str, trusted_origin: str | None = None
    ) -> Any:
        return SimpleNamespace(subject=params["from"])


def _harness(*, spaces: tuple[str, ...] = ("s1",), **kwargs: Any) -> Any:
    fs = FakeOrgFs()
    for space in spaces:
        fs.add_space(space)
    h = make_service(fs=fs, **kwargs)
    return h


def _validate(spec: Any, result: Any) -> None:
    errors = list(Draft202012Validator(spec.result_schema).iter_errors(result))
    assert not errors, (spec.name, errors, result)


def test_real_non_empty_list_and_stats_results_match_the_declarations() -> None:
    h = _harness()
    establish_many(h.fs, [(A, B), (A, C)], now_ms=1_000_000)
    h.store.refresh()
    h.tick()
    now = h.clock.now_ms()
    h.service.submitted(
        sender=A, target=B, message_id="m-1", reply_to=None, hold_reason=None, now_ms=now
    )
    h.service.injected(
        actor=A, message_id="in-1", sender=B, intent="request", reply_to=None,
        received_at_ms=now,
    )
    h.service.reply_received(
        actor=A, reply_message_id="r-1", reply_to="in-1", sender=B,
        received_at_ms=now + 5,
    )
    h.tick(now + 10)

    listed = link_catalog.link_list(_App(h.service), {"from": A})
    stats = link_catalog.link_stats(_App(h.service), {"from": A})
    _validate(link_catalog.LINK_LIST_SPEC, listed)
    _validate(link_catalog.LINK_STATS_SPEC, stats)

    assert listed["links"], listed
    assert listed["owner"], listed
    assert stats["links"], stats
    row = stats["links"][0]
    assert row["peer"] and row["inFlight"] == 1


def test_a_real_held_row_matches_the_declared_shape() -> None:
    h = _harness()
    establish(h.fs, A, B)
    h.store.refresh()
    h.tick()
    now = h.clock.now_ms()
    h.inbox.hold(
        "held-1",
        sender=A,
        target=C,
        reason=ipc_errors.LINK_WINDOW_FULL,
        created_at_ms=now,
        expires_at_ms=now + 600_000,
    )
    h.service.submitted(
        sender=A,
        target=C,
        message_id="held-1",
        reply_to=None,
        hold_reason=ipc_errors.LINK_WINDOW_FULL,
        now_ms=now,
    )
    h.tick(now)

    listed = link_catalog.link_list(_App(h.service), {"from": A})
    _validate(link_catalog.LINK_LIST_SPEC, listed)
    (held,) = listed["held"]
    assert held["messageId"] == "held-1"
    assert held["target"] == C
    assert held["holdReason"] in HOLD_REASONS
    assert isinstance(held["detail"], str)
    assert isinstance(held["heldMs"], int)


def test_every_real_add_branch_matches_the_change_declaration() -> None:
    h = _harness()
    establish(h.fs, A, B)
    h.store.refresh()
    h.tick()
    app = _App(h.service)
    now = h.clock.now_ms()

    existing = link_catalog.link_add(app, {"from": A, "peer": B})
    _validate(link_catalog.LINK_ADD_SPEC, existing)
    assert existing["state"] == "established"

    minted = h.service.add(actor=A, peer=C, now_ms=now)
    _validate(link_catalog.LINK_ADD_SPEC, {"ok": True, **minted})
    assert minted["state"] == "pending"

    dropped = link_catalog.link_drop(app, {"from": A, "peer": C})
    _validate(link_catalog.LINK_DROP_SPEC, dropped)
    assert dropped["state"] == "reclaiming"

    absent = link_catalog.link_drop(app, {"from": A, "peer": B})
    _validate(link_catalog.LINK_DROP_SPEC, absent)
    assert absent["state"] in {"reclaiming", "absent"}


def test_without_org_the_real_hold_and_bypass_branches_match() -> None:
    empty = _harness(spaces=())
    empty.store.refresh()
    empty.tick()
    held = empty.service.add(actor=A, peer=B, now_ms=empty.clock.now_ms())
    _validate(link_catalog.LINK_ADD_SPEC, {"ok": True, **held})
    assert held["state"] == "absent" and held["holdReason"]

    bypassed = _harness(bypass=True)
    bypassed.store.refresh()
    bypassed.tick()
    not_recorded = bypassed.service.add(actor=A, peer=B, now_ms=bypassed.clock.now_ms())
    _validate(link_catalog.LINK_ADD_SPEC, {"ok": True, **not_recorded})
    assert not_recorded["state"] == "not-recorded"


def test_a_real_one_sided_reference_also_validates() -> None:
    h = _harness()
    write_reference(h.fs, A, {B: LinkEntry(peer=B, minted_at_ms=1, revision=1)})
    h.store.refresh()
    h.tick()
    listed = link_catalog.link_list(_App(h.service), {"from": A})
    _validate(link_catalog.LINK_LIST_SPEC, listed)
    assert listed["links"][0]["peer"] == B
