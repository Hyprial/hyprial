"""Upgrade-safety properties the legacy workflow cutover must hold.

These were written by review (infra-ops, 2026-09-23) BEFORE the fix, and are
red on 3eb89c20.  They pin properties, not today's implementation:

1. Downgrade: autoupdate can move a node back to the previous tag.  The old
   ``WorkflowRegistry.__init__`` re-saves every persisted effect batch with
   ``INSERT OR REPLACE`` (7324b4f5 ``workflow/registry.py:350-357`` ->
   ``workflow/store.py:994-1016``).  Whatever rows the cutover leaves in the
   sealed archive, that replay must not be rejected -- otherwise the old
   daemon raises at ``_start_runtime`` and dies before ``autoupdate.start``,
   so the node can never update itself forward again.
2. Startup: the cutover and the remote-workflow wiring run on every node at
   the same moment after this merge.  A failure in either (locked, corrupt or
   read-only database; a PacError from a migration) must degrade the daemon,
   not abort ``_start_runtime``.  Like ``test_daemon_restart_ownership``, this
   is asserted at source level: a behavioural double of ``_start_runtime``
   would have to fake a real Zenoh transport.
3. Silence: cancelling a still-running legacy run must leave a record an
   operator and the submitter can act on -- which runs, submitted by whom --
   not only a count.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.wiring.services import _WiringServicesMixin
from hyprial.daemon.impl.application.wiring.transport import _WiringTransportMixin
from hyprial.daemon.impl.pac.storage.legacy import cutover

# The two effect tables exactly as the previous release creates them
# (7324b4f5 src/hyprial/workflow/store.py:158-171).
_OLD_EFFECT_DDL = """
CREATE TABLE IF NOT EXISTS workflow_effect_batches (
    correlation_id TEXT PRIMARY KEY,
    final_kind TEXT NOT NULL,
    final_payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_effects (
    effect_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (correlation_id) REFERENCES workflow_effect_batches(correlation_id)
);
CREATE INDEX IF NOT EXISTS workflow_effects_batch ON workflow_effects(correlation_id);
"""


def _old_home_with_pending_effect_batch(state_dir: Path) -> None:
    db = sqlite3.connect(state_dir / "workflows.sqlite3")
    db.executescript(
        """
        CREATE TABLE runs(run_id TEXT PRIMARY KEY,name TEXT,state TEXT,sender TEXT,
                          created_at_ms INTEGER,finished_at_ms INTEGER,yaml_text TEXT);
        CREATE TABLE targets(run_id TEXT,target TEXT,state TEXT,reply_excerpt TEXT);
        INSERT INTO runs VALUES ('run-live','live','running','agent:o:n:submitter',1,NULL,'body');
        """
        + _OLD_EFFECT_DDL
    )
    effect = {"effect_id": "eff-1", "generation": 3, "kind": "timer"}
    db.execute(
        "INSERT INTO workflow_effect_batches VALUES (?,?,?)",
        ("corr-1", "WorkflowTimerCompleted", json.dumps({"runId": "run-live"})),
    )
    db.execute(
        "INSERT INTO workflow_effects VALUES (?,?,?,?)",
        ("eff-1", "corr-1", 3, json.dumps(effect, sort_keys=True)),
    )
    db.commit()
    db.close()


def _replay_like_the_previous_release(state_dir: Path) -> None:
    """What the old registry does with every persisted batch on start."""

    db = sqlite3.connect(state_dir / "workflows.sqlite3")
    try:
        with db:
            batches = db.execute(
                "SELECT correlation_id, final_kind, final_payload_json FROM workflow_effect_batches"
            ).fetchall()
            for correlation_id, final_kind, final_payload in batches:
                db.execute(
                    "INSERT OR REPLACE INTO workflow_effect_batches "
                    "(correlation_id, final_kind, final_payload_json) VALUES (?, ?, ?)",
                    (correlation_id, final_kind, final_payload),
                )
                for effect_id, generation, payload in db.execute(
                    "SELECT effect_id, generation, payload_json FROM workflow_effects "
                    "WHERE correlation_id = ?",
                    (correlation_id,),
                ).fetchall():
                    db.execute(
                        "INSERT OR REPLACE INTO workflow_effects "
                        "(effect_id, correlation_id, generation, payload_json) "
                        "VALUES (?, ?, ?, ?)",
                        (effect_id, correlation_id, generation + 1, payload),
                    )
    finally:
        db.close()


def test_replay_control_succeeds_before_cutover(tmp_path: Path) -> None:
    # Control: the replay itself is valid against an un-cut-over home, so a
    # failure below is caused by the cutover, not by the simulated old code.
    _old_home_with_pending_effect_batch(tmp_path)
    _replay_like_the_previous_release(tmp_path)


def test_previous_release_can_still_start_after_cutover(tmp_path: Path) -> None:
    _old_home_with_pending_effect_batch(tmp_path)
    cutover(tmp_path, at=100)
    # Must not raise: IntegrityError WORKFLOW_RETIRED here is a crash loop
    # on every downgraded node that had an in-flight effect at upgrade.
    _replay_like_the_previous_release(tmp_path)


def _start_runtime_tree() -> ast.FunctionDef:
    # The runtime entry delegates the original guarded block to this exact
    # native MRO owner. A disconnected or shadowed phase must not pass.
    if (DaemonApplication._start_runtime is not _WiringTransportMixin._start_runtime
            or DaemonApplication._startup_adopt_domain_services
            is not _WiringServicesMixin._startup_adopt_domain_services):
        raise AssertionError("startup workflow phase no longer resolves to its native owner")
    entry, = ast.parse(textwrap.dedent(inspect.getsource(DaemonApplication._start_runtime))).body
    if not any(isinstance(node, ast.Call)
               and ast.unparse(node) == "self._startup_adopt_domain_services(graph)"
               for node in ast.walk(entry)):
        raise AssertionError("native workflow phase is no longer called from _start_runtime")
    source = textwrap.dedent(inspect.getsource(DaemonApplication._startup_adopt_domain_services))
    tree = ast.parse(source)
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    return function


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    """Calls whose callee is ``name``; a dotted name also pins the owner attribute."""

    owner, _, attr = name.rpartition(".")
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called != attr:
                continue
            if owner:
                base = getattr(func, "value", None)
                if getattr(base, "attr", None) != owner:
                    continue
            found.append(child)
    return found


def _handler_catches_broadly(handler: ast.ExceptHandler) -> bool:
    names: list[str] = []
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for kind in kinds:
        if kind is None:
            return True  # bare except
        if isinstance(kind, ast.Name):
            names.append(kind.id)
        elif isinstance(kind, ast.Attribute):
            names.append(kind.attr)
    return "Exception" in names or "BaseException" in names


def _guarded(function: ast.FunctionDef, call: ast.Call) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.Try):
            continue
        inside = any(call in list(ast.walk(statement)) for statement in node.body)
        if inside and any(_handler_catches_broadly(h) for h in node.handlers):
            return True
    return False


# ``_workflow_service.recover`` guards only its own open(); its COUNT query and
# submit_timer can still raise (found by e2e-verifier, 2026-09-23).
@pytest.mark.parametrize("step", ["cutover", "RemoteWorkflow", "_workflow_service.recover"])
def test_startup_degrades_when_a_workflow_wiring_step_fails(step: str) -> None:
    function = _start_runtime_tree()
    calls = _calls_named(function, step)
    # Denominator: the step must actually be wired here, or this test would
    # pass vacuously after a refactor that moved it.
    assert calls, f"{step}(...) is no longer called from _start_runtime; move this guard with it"
    unguarded = [call.lineno for call in calls if not _guarded(function, call)]
    assert not unguarded, (
        f"{step}(...) in _start_runtime is not inside a try that catches Exception "
        f"(relative lines {unguarded}); a locked/corrupt/read-only database or a PacError "
        "would abort startup on every node at once, before autoupdate can run"
    )


def test_guard_detector_recognises_guarded_and_unguarded_calls() -> None:
    # Positive/negative control for the source-level detector above.
    tree = ast.parse(
        textwrap.dedent(
            """
            def f():
                a = step()
                try:
                    b = step()
                except Exception:
                    pass
                try:
                    c = step()
                except OSError:
                    pass
            """
        )
    )
    function = tree.body[0]
    verdicts = [_guarded(function, call) for call in _calls_named(function, "step")]
    assert verdicts == [False, True, False]


def test_cutover_names_the_runs_it_cancelled_and_their_submitters(tmp_path: Path) -> None:
    _old_home_with_pending_effect_batch(tmp_path)
    result = cutover(tmp_path, at=100)
    assert result["terminated"] == 1
    cancelled = result.get("cancelled")
    assert cancelled is not None, (
        "cutover returns only a count; nothing can tell the operator or the submitter "
        "which in-flight runs were cancelled"
    )
    assert [(entry["runId"], entry["sender"]) for entry in cancelled] == [
        ("run-live", "agent:o:n:submitter")
    ]
