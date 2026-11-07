'U0BRACE (PR #332 fix set, 2026-09-04): every declared row has exactly\none restart owner.\n\nThe adjudication (hq-adjutant, 四格裁定 ②): the bootstrap/restore clash is\nnot a timing race but a DOUBLE OWNER.  ``_bootstrap_lifecycle_harnesses``\nskipped ``harness == "lark"`` and ``status != "running"``;\n``_restore_harnesses`` declared exactly ``harness != "lark" and\nstatus == "running"`` -- the same predicate twice, so in steady state every\nnon-Lark running row had TWO submitters and only arrival order decided who\nstarted the process.  The loser either deferred (F1 made that settle, but\nsettling a handoff is not owning the row) or -- the harm -- landed its claim\nafter ``_on_restore`` had reset ``_records``, and the restore batch started a\nSECOND process for a row whose first process nothing would ever stop.\n\nThe fix is structural, in the shape the Lark half already had: partition the\nselection surface.  ``_restore_adapters`` owns the ``lark`` rows,\n``_restore_harnesses`` owns every other ``running`` row, and the\nconstruction-time saga resubmission -- the second owner -- is deleted.\nDurable rows, agent bindings and persona routes are re-derived at\nconstruction from their registries (``_seed_agent_bindings``,\n``_restore_persona_routes``); a restart only ever owes the processes, and it\nowes them to the restore batch.\n\nTwo assertions live here, and they answer the two halves of "exactly one\nowner":\n\n* the WIRING half -- the startup window (``run()`` and the steps it drives)\n  submits no lifecycle operations at all, so no second owner can exist no\n  matter the timing.  This is a source-level assertion for the same reason\n  ``test_lark_lifecycle_single_writer`` is: the defect was a call that only\n  existed in one place, and a behavioral double of ``_start_runtime`` would\n  have to fake a real Zenoh transport to see it;\n* the ROW half -- the restore batch is handed exactly the non-Lark running\n  rows, and the lifecycle manager is handed nothing for them.\n\nF1\'s settlement doors (``tests/daemon/application/restore/test_daemon_restore_settlement.py``) stay as\nthey are: a lifecycle claim CAN still overlap a restore target when a\nuser-initiated saga (``hyprial agent start`` racing the restore window, or U0c\ncompensating a saga interrupted by the restart) claims a key, and those\nclaims must keep settling as deferred handoffs.\n'

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessRestoreProjection


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

NODE = "ownership-node"
OWNER = "tester"

# The startup window, definitionally shared with
# ``tests/test_startup_event_reconciliation.py``: ``run()`` (including the
# nested ``step()`` wrapper and the failure-path teardown) and the steps it
# drives.  The scanner follows direct ``self._*()`` method calls transitively
# from these roots, so helpers like ``_seed_agent_bindings`` are covered
# without being named.
_STARTUP_ROOTS = frozenset(
    {
        "run",
        "_restore_adapters",
        "_restore_harnesses",
        "_start_runtime",
        "_start_server",
    }
)

# Methods reachable from the roots that are definitionally outside the
# startup window (the same boundary decisions the reconciliation scanner
# made; duplicated rather than imported because that module's copies are
# test-local constants, not a shared contract).
_STARTUP_BOUNDARIES = frozenset(
    {
        "_serve",
        "_start_ipc_client",
        "_start_maintenance_scheduler",
        "_schedule_maintenance",
        "_scheduled_maintenance",
    }
)

# What a second owner looks like in source: submitting a lifecycle operation
# (a create saga's harness step is a process start), or reaching for the
# helper that submits one.  Port WIRING (``_start_lifecycle_manager`` hands
# domain ports to the manager) does not appear here -- wiring is not
# submission.
_SECOND_OWNER_MARKERS = ("LifecycleOperation", "_run_lifecycle_operation")


def _application_startup_source() -> str:
    """Concatenated source of DaemonApplication and its cluster mixins.

    Since the application split the class is a thin composition root; the
    startup window spans every mixin's methods, so shape scans read them all.
    """

    parts = []
    for cls in reversed(DaemonApplication.__mro__):
        if cls is object or not cls.__name__.startswith("_"):
            continue
        try:
            parts.append(inspect.getsource(cls))
        except (OSError, TypeError):
            continue
    return "\n\n".join(parts)


def _application_methods() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    tree = ast.parse(_application_startup_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(item.name, item)
    return methods


def _startup_call_graph() -> frozenset[str]:
    """Direct ``self._*()`` calls reachable from the startup roots."""

    methods = _application_methods()
    reached: set[str] = set()
    stack = sorted(_STARTUP_ROOTS)
    while stack:
        name = stack.pop()
        if name in reached or name in _STARTUP_BOUNDARIES:
            continue
        reached.add(name)
        function = methods.get(name)
        if function is None:
            # Roots may be renamed; a missing root must fail loudly, not
            # scan nothing and pass.
            raise AssertionError(
                f"startup root {name}() is not a DaemonApplication method; "
                "if it was renamed, rename the root in this test too"
            )
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("_")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                callee = node.func.attr
                if callee in methods and callee not in reached:
                    stack.append(callee)
    return frozenset(reached)


# --------------------------------------------------------------------------- #
# The wiring half: no second owner can be constructed.
# --------------------------------------------------------------------------- #


def test_the_startup_window_submits_no_lifecycle_operations() -> None:
    """No method reachable from ``run()`` submits a lifecycle operation.

    On the pre-fix tree this was red in the most literal way:
    ``_start_runtime`` called ``_bootstrap_lifecycle_harnesses``, whose body
    read ``manager.submit(LifecycleOperation.create(...))`` for every
    non-Lark running row -- the same rows ``_restore_harnesses`` declares.
    The assertion is on names, not behaviour, because the defect WAS a name:
    one call site composing a second submitter.  User-initiated sagas
    (``hyprial agent start``, transfers) submit from ``handle()``/command paths, which
    are behind ``_serve`` -- a boundary here -- and behind the
    ``DAEMON_RESTORING`` gate at runtime, so they are not startup owners.
    """

    methods = _application_methods()
    offenders: list[str] = []
    for name in sorted(_startup_call_graph()):
        body = ast.dump(methods[name])
        for marker in _SECOND_OWNER_MARKERS:
            if marker in body:
                offenders.append(f"{name}() references {marker}")
    assert not offenders, (
        "the daemon startup window submits lifecycle operations -- a method "
        "reachable from run() may not be a second owner of desired rows: "
        + "; ".join(offenders)
    )


def test_mutation_a_reintroduced_bootstrap_submission_is_caught() -> None:
    """The scanner has teeth: re-adding a submitting call must go red.

    This is the shape of the removed defect, re-inserted verbatim into a
    method the startup window reaches.
    """

    source = _application_startup_source()
    marker = "    def _seed_agent_bindings(self) -> None:"
    assert marker in source
    call = (
        "        self._run_lifecycle_operation(\n"
        "            LifecycleOperation.create(\n"
        '                "bootstrap:e:codex:ghost", self._lifecycle_spec(spec)\n'
        "            )\n"
        "        )\n"
    )
    index = source.index(marker) + len(marker)
    mutated = source[:index] + "\n" + call + source[index:]
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    tree = ast.parse(mutated)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(item.name, item)
    offenders = [
        f"{name}() references {marker_name}"
        for name in sorted(_startup_call_graph())
        for marker_name in _SECOND_OWNER_MARKERS
        if marker_name in ast.dump(methods[name])
    ]
    assert offenders, "a reintroduced startup-side submission went unseen"
    assert any("LifecycleOperation" in item for item in offenders)


# --------------------------------------------------------------------------- #
# The row half: restore owns exactly the declared running rows.
# --------------------------------------------------------------------------- #


class _RecordingHarnesses:
    """Records what the composer asks to be restored.

    The real summary type, not a fabricated shape: restore's contract is
    ``HarnessRestoreProjection`` and the daemon's recovery log reads its
    fields -- the same ruling F2 wrote down for launch results.
    """

    def __init__(self) -> None:
        self.restored: list[tuple[str, str]] = []

    def restore(self, desired: tuple[Any, ...]) -> HarnessRestoreProjection:
        self.restored.extend((spec.harness, spec.name) for spec in desired)
        return HarnessRestoreProjection(
            attempted=len(desired), restored=len(desired), failed=0, deferred=0
        )


class _RecordingManager:
    """The lifecycle manager seam: any submission during restart is a second
    owner, so this double's whole job is to have witnessed none."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, operation: Any) -> PortAdmission:
        self.submitted.append(str(operation.operation_id))
        return PortAdmission.ACCEPTED

    def drain(self, _timeout: float) -> bool:
        return True


def _write_desired(state_dir: Path, providers: list[dict[str, Any]]) -> None:
    # Seeded BEFORE the store constructs: construction runs the .v1 -> SQLite
    # migration (U0a-2).
    state_dir.mkdir(parents=True, exist_ok=True)
    Path(state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": providers,
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )


def _provider(harness: str, name: str, status: str = "running") -> dict[str, Any]:
    return {
        "provider": harness,
        "name": name,
        "headless": True,
        "args": [],
        "ownership": "managed",
        "status": status,
    }


def _application(tmp_path: Path, providers: list[dict[str, Any]]) -> DaemonApplication:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state_dir = home / "state"
    _write_desired(state_dir, providers)
    return DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id=NODE,
        hyprial_home=home,
        owner=OWNER,
        **_app_injections(),
    )


def test_each_declared_row_has_exactly_one_restart_owner(
    tmp_path: Path, own_daemon_application
) -> None:
    """The partition itself, asserted with both kinds of row in the mix.

    "the pi row was restored" alone is equally explained by "everything is
    restored"; "nothing was submitted to the lifecycle manager" alone is
    equally explained by "the manager was never wired".  The declaration
    carries a running row, a failed row and a Lark row, and BOTH owners are
    watched, so what the assertion pins is the split: restore receives
    exactly the running non-Lark row, the manager receives nothing, Lark
    belongs to the adapter half.  Together with the wiring test above (which
    is what went red while the construction-phase submitter still existed)
    this is "each declared row has exactly one owner": the one owner is
    observable receiving the row, and no other owner can be constructed.
    """

    app = _application(
        tmp_path,
        [
            _provider("pi", "squire"),
            _provider("codex", "sickly", status="failed"),
            _provider("lark", "alicesmith-hyprial-e2e"),
        ],
    )
    own_daemon_application(app)
    harnesses = _RecordingHarnesses()
    app._harnesses = harnesses  # type: ignore[assignment]
    manager = _RecordingManager()
    app._lifecycle_manager = manager  # type: ignore[assignment]

    app._restore_harnesses()

    assert harnesses.restored == [("pi", "squire")], (
        "the restore batch is the ONE owner of non-Lark running rows: "
        f"expected exactly the running row, saw {harnesses.restored}"
    )
    assert manager.submitted == [], (
        "the lifecycle manager was handed operations during restart -- a "
        "desired row would have two owners: "
        f"{manager.submitted}"
    )
