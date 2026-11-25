"""Acceptance (c): body retention changes by writing cell data only.

Phase A promise (design §10.0, S7): body retention is read from cell data,
so an owner changes it by writing an agent cell's ``params`` and nothing
else: no code, config or env change.

The cell data is the stored OrgFS document.  ``OrgCellDocs`` hand-writes
``hyprial-cell/v1`` documents at ``cells/<kind>/<c.id>.json`` in the org
directory space of the app's own ``LocalOrgFs`` and edits them in place
(next revision, same path); no write goes through ``CellRuntime``, so a
retention can only follow a document the runtime's OrgFS scan read back.

The daemon boots like ``application/wiring/services.py``:
``bind_body_retention_params``, then the conversation recorder.  ``_settle``
runs one explicit full pass so the test does not wait for the runtime's own
interval; composition also starts the runtime thread and ``kick`` wakes it,
so background passes may run too (``_pass_lock`` serialises them), and the
assertions hold whichever pass reads the document first.

PIN: passed on first run.  Seeded red cases (temporary product edits,
reverted): ``default_retention`` resolving with snapshot None ->
``604800000 != 18000000``; ``CellRuntime._refresh`` skipping every eager
scan copy -> the agent retention is never seen.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.cells.params import DELIVERY_BODY_RETENTION_MS
from hyprial.daemon.impl.composition.cells import cell_runtime
from tests.daemon.acceptance.acceptance_support import (
    HOUR_MS,
    OrgCellDocs,
    body_columns,
    boot_like_services,
    send,
)
from tests.support.golden.app import (
    ALPHA,
    BETA,
    OWNER_USER,
    golden_daemon as golden_daemon,
)

RETENTION = DELIVERY_BODY_RETENTION_MS.key


def _settle(runtime: Any) -> None:
    runtime.run_once()


def _held_params(runtime: Any, cell: Any) -> Any:
    held = runtime.lookup(cell.cell_id)
    assert held is not None, f"the runtime never read {cell.kind} {cell.cell_id}"
    return held.param(RETENTION), held.revision


def test_body_retention_follows_agent_cell_documents(
    golden_daemon, monkeypatch
) -> None:
    daemon, recorder = boot_like_services(
        golden_daemon(bypass=False, links=False), monkeypatch, crowd=True
    )
    app = daemon.app
    docs = OrgCellDocs(app)
    first = send(app, OWNER_USER, ALPHA, "default")["messageId"]
    assert recorder.run_once().failed is False
    runtime = cell_runtime(app)
    assert runtime is app._cell_runtime and runtime is not None
    assert body_columns(app)[first]["retentionMs"] == DELIVERY_BODY_RETENTION_MS.default

    _settle(runtime)
    alpha = docs.agent(app.agents.get("alpha"), {RETENTION: 5 * HOUR_MS})
    _settle(runtime)
    assert _held_params(runtime, alpha) == (5 * HOUR_MS, 2)
    second = send(app, OWNER_USER, ALPHA, "alpha-5h")["messageId"]
    other = send(app, OWNER_USER, BETA, "other-agent")["messageId"]
    assert recorder.run_once().failed is False
    rows = body_columns(app)
    assert rows[second]["retentionMs"] == 5 * HOUR_MS
    assert rows[other]["retentionMs"] == DELIVERY_BODY_RETENTION_MS.default

    alpha = docs.set_params(alpha, {RETENTION: 2 * HOUR_MS})
    _settle(runtime)
    assert _held_params(runtime, alpha) == (2 * HOUR_MS, 3)
    third = send(app, OWNER_USER, ALPHA, "alpha-2h")["messageId"]
    assert recorder.run_once().failed is False
    assert body_columns(app)[third]["retentionMs"] == 2 * HOUR_MS

    alpha = docs.set_params(alpha, {RETENTION: HOUR_MS})
    assert docs.stored_params(alpha) == {RETENTION: HOUR_MS}
    _settle(runtime)
    fourth = send(app, OWNER_USER, ALPHA, "tighter")["messageId"]
    assert recorder.run_once().failed is False
    rows = body_columns(app)
    assert rows[fourth]["retentionMs"] == HOUR_MS
    assert rows[first]["retentionMs"] == DELIVERY_BODY_RETENTION_MS.default
    assert runtime.dirty() == frozenset(), "the runtime left a cell unflushed"
