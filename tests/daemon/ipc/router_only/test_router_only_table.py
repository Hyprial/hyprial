"""The router-only table equals the router's own branches.

Decision: daemon-first-z4b-router-only-refusal-20261117.  The table is compared
with the names the dispatch source answers, in both directions, so a branch
added or removed without the table changing fails here; mutated copies of the
real source (never the repository file) prove it.
"""

from __future__ import annotations

import inspect

from hyprial.daemon.impl.application.actors.agents_admin import (
    migration as agents_migration,
)
from hyprial.daemon.impl.ipc import router as ipc_router
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.migration import ROUTER_ONLY_METHODS

from .router_branches import disagreement, router_branch_names

REGISTERED = frozenset(METHOD_REGISTRY.names)
ANCHOR = '        if method == "outbox.prune":\n'
ROUTER_SOURCE = inspect.getsource(ipc_router)
MIGRATION_SOURCE = inspect.getsource(agents_migration)


def test_the_table_names_are_not_declarations() -> None:
    assert ROUTER_ONLY_METHODS.isdisjoint(REGISTERED)
    assert len(ROUTER_ONLY_METHODS) == 60


def test_the_table_equals_the_branches_of_the_router() -> None:
    assert disagreement(router_branch_names(), REGISTERED, ROUTER_ONLY_METHODS) == ([], [])


def test_a_branch_added_to_the_router_is_reported_missing_from_the_table() -> None:
    assert ANCHOR in ROUTER_SOURCE
    added = ROUTER_SOURCE.replace(
        ANCHOR,
        '        if method == "scratch.added":\n            return None\n' + ANCHOR,
        1,
    )
    names = router_branch_names({ipc_router.__name__: added})
    assert disagreement(names, REGISTERED, ROUTER_ONLY_METHODS) == (["scratch.added"], [])


def test_a_branch_removed_from_the_router_is_reported_stale_in_the_table() -> None:
    removed = ROUTER_SOURCE.replace(ANCHOR, '        if method == "scratch.renamed":\n', 1)
    names = router_branch_names({ipc_router.__name__: removed})
    missing, stale = disagreement(names, REGISTERED, ROUTER_ONLY_METHODS)
    assert stale == ["outbox.prune"] and missing == ["scratch.renamed"]


def test_a_branch_added_to_the_agent_migration_handler_is_reported() -> None:
    anchor = 'elif method in {"agent.migrate.execute", "agent.migrate.rollback"}:'
    assert anchor in MIGRATION_SOURCE
    added = MIGRATION_SOURCE.replace(
        anchor, 'elif method == "agent.migrate.scratch":\n                pass\n            ' + anchor, 1
    )
    names = router_branch_names({agents_migration.__name__: added})
    assert disagreement(names, REGISTERED, ROUTER_ONLY_METHODS) == (
        ["agent.migrate.scratch"],
        [],
    )


def test_a_name_that_gets_registered_must_leave_the_table() -> None:
    names = router_branch_names()
    registered_now = REGISTERED | {"outbox.prune"}
    assert disagreement(names, registered_now, ROUTER_ONLY_METHODS) == ([], ["outbox.prune"])
