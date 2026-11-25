"""The trusted source set: owner-authored agent cells only (Q10, contract OC0 §2.5)."""

from __future__ import annotations

from hyprial.daemon.impl.cells.params import (
    DELIVERY_BODY_RETENTION_MS,
    PARAM_SOURCE_KINDS,
    ParamSpec,
    declare_param,
    resolve_param,
    resolve_param_detail,
)
from hyprial.kernel import subject_cell_id

from tests.daemon.cells.params.param_support import (
    AGENT_ID,
    AGENT_URI,
    OTHER_AGENT_ID,
    OTHER_AGENT_URI,
    OWNER,
    STRANGER,
    agent,
    org_cell,
    snapshot,
)

KEY = DELIVERY_BODY_RETENTION_MS.key
HOUR_MS = 3_600_000


def test_only_agent_cells_are_param_sources() -> None:
    assert PARAM_SOURCE_KINDS == frozenset({"agent"})


def test_an_org_cell_is_never_a_param_source() -> None:
    view = snapshot(
        agent(),
        org_cell(),
    )
    assert resolve_param(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 604_800_000
    assert view.sources == ()
    assert [(item.kind, item.authority, item.reason) for item in view.rejected] == [
        ("org", OWNER, "kind")
    ]


def test_a_foreign_authority_agent_cell_is_rejected() -> None:
    view = snapshot(
        agent({KEY: 2 * HOUR_MS}, authority=STRANGER),
    )
    assert resolve_param(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 604_800_000
    assert view.sources == ()
    assert [(item.kind, item.authority, item.reason) for item in view.rejected] == [
        ("agent", STRANGER, "authority")
    ]


def test_a_foreign_agent_cell_neither_contributes_nor_aliases_the_subject() -> None:
    foreign = agent({KEY: 2 * HOUR_MS}, authority=STRANGER)
    view = snapshot(foreign)
    assert resolve_param(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 604_800_000
    assert resolve_param(view, DELIVERY_BODY_RETENTION_MS, foreign.cell_id) == 604_800_000


def test_retired_cells_are_skipped_and_cells_without_params_are_not_rejected() -> None:
    view = snapshot(
        agent(),
        agent({KEY: 2 * HOUR_MS}, agent_id=OTHER_AGENT_ID, uri=OTHER_AGENT_URI, retired=True),
    )
    assert resolve_param(view, DELIVERY_BODY_RETENTION_MS, AGENT_URI) == 604_800_000
    assert view.rejected == ()


def test_the_snapshot_carries_the_index_generation_and_owner() -> None:
    view = snapshot(agent({KEY: 2 * HOUR_MS}), generation=9)
    assert view.generation == 9
    assert view.owner == OWNER
    assert view.available
    assert [source.kind for source in view.sources] == ["agent"]


def test_agents_sharing_a_member_ref_do_not_merge_their_params() -> None:
    limit = declare_param(
        ParamSpec("tests.sharedLimit", "int", combine="min", default=10)
    )
    view = snapshot(
        agent({limit.key: 5}, extra_members=(OWNER,)),
        agent(
            {limit.key: 1},
            agent_id=OTHER_AGENT_ID,
            uri=OTHER_AGENT_URI,
            extra_members=(OWNER,),
        ),
    )
    mine = resolve_param_detail(view, limit, AGENT_URI)
    assert mine.value == 5
    assert mine.sources == (subject_cell_id("agent", AGENT_ID),)
    theirs = resolve_param_detail(view, limit, OTHER_AGENT_URI)
    assert theirs.value == 1
    assert theirs.sources == (subject_cell_id("agent", OTHER_AGENT_ID),)


def test_a_non_harness_member_ref_does_not_name_the_agent() -> None:
    limit = declare_param(
        ParamSpec("tests.sharedLimit", "int", combine="min", default=10)
    )
    worker = agent({limit.key: 5}, extra_members=(OWNER,))
    view = snapshot(worker)
    assert resolve_param(view, limit, OWNER) == 10
    assert view.subject_refs(AGENT_URI) == frozenset(
        {worker.cell_id, AGENT_ID, AGENT_URI}
    )


def test_a_harness_uri_claimed_by_two_agents_names_neither() -> None:
    limit = declare_param(
        ParamSpec("tests.sharedLimit", "int", combine="min", default=10)
    )
    view = snapshot(
        agent({limit.key: 5}),
        agent({limit.key: 1}, agent_id=OTHER_AGENT_ID),
    )
    assert view.ambiguous == frozenset({AGENT_URI})
    assert resolve_param(view, limit, AGENT_URI) == 10
    assert resolve_param(view, limit, AGENT_ID) == 5
    assert resolve_param(view, limit, OTHER_AGENT_ID) == 1
