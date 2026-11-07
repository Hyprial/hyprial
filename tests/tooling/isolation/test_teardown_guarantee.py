"""Everything that waits for a shutdown must wait at least as long as it takes.

On 2026-08-31 an autoupdate installed cleanly, stopped the running daemon, and
never started a replacement. Production was down for 56 minutes. The report
said:

    daemon restart failed: daemon socket closed but teardown did not finish
    within 10s (lock is still held)

The daemon was not broken. Teardown took 20.5s, which is inside what it may
take, and the outgoing process did exit. Three separate callers were waiting
for that exit, each with a number it had invented for itself -- 10s for the
receipt, 15s for the replacement's lock, 15s for the daemon's own backstop --
and none of them could have derived the right one, because **the thing being
waited on had no stated bound at all**.

🔑 So the defect was not a number that was too small. It was a number that did
not exist, which left every waiter guessing, and a wrong guess raised nothing.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import ast
import re
from pathlib import Path

import pytest
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

from hyprial.kernel import TEARDOWN_BUDGETED_SECONDS
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.shutdown import (
    _CLOSE_BUDGET_SECONDS,
    _CLOSE_STEP_BUDGETS,
    _CLOSE_UNBUDGETED_STEPS,
    _EXIT_BACKSTOP_SECONDS,
    _RESTORE_THREAD_JOIN_TIMEOUT,
    _TURN_HOOK_CLOSE_TIMEOUT,
)
from hyprial.daemon.impl.application.netendpoints.endpoints import _lock_wait_timeout
from hyprial.daemon.impl.bootstrap.discovery import DISCOVERY_TIMEOUT_SECONDS
from hyprial.daemon.impl.configuration.home_guard import (
    DEFAULT_DUPLICATE_OBSERVE_WINDOW,
    DUPLICATE_OBSERVE_MARGIN_SECONDS,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

ROOT = Path(__file__).resolve().parents[3]


def _native_service_call_tree(source: str) -> ast.Module:
    """Recognize only the exact native get_services -> local services binding."""
    tree = ast.parse(source)
    providers = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 and any((alias.asname or alias.name) == "get_services" for alias in node.names)]
    if not providers or any(node.level or node.module != "hyprial.shell.impl.cli.commands.common.services"
                            or any(alias.name != "get_services" or alias.asname
                                   for alias in node.names) for node in providers):
        return tree
    if any((isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            and node.id == "get_services")
           or (isinstance(node, ast.arg) and node.arg == "get_services")
           or (isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == "get_services")
           for node in ast.walk(tree)):
        return tree
    for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
        bindings = [node for node in function.body if isinstance(node, ast.Assign)
                    and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "services" and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id == "get_services"
                    and not node.value.args and not node.value.keywords]
        stores = [node for node in ast.walk(function) if isinstance(node, ast.Name)
                  and isinstance(node.ctx, ast.Store) and node.id == "services"]
        if (len(bindings) != 1 or len(stores) != 1
                or any(isinstance(node, ast.arg) and node.arg == "services" for node in ast.walk(function))
                or any(isinstance(node, (ast.Import, ast.ImportFrom))
                       and any((alias.asname or alias.name.split('.')[0]) == "services"
                               for alias in node.names) for node in ast.walk(function))):
            continue
        for call in [node for node in ast.walk(function) if isinstance(node, ast.Call)]:
            if (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "services" and call.func.attr in {
                        "_stop_daemon_gracefully", "_launch_daemon_process"}):
                call.func = ast.copy_location(ast.Name(id=call.func.attr, ctx=ast.Load()), call.func)
    return tree


def _native_restart_source() -> str:
    from hyprial.shell.impl.cli.commands.common import (
        daemon_start,
        daemon_stop,
        services,
    )

    actual = services.get_services()
    assert actual._stop_daemon_gracefully is daemon_stop._stop_daemon_gracefully
    assert actual._launch_daemon_process is daemon_start._launch_daemon_process
    assert services.get_services()._stop_daemon_gracefully is actual._stop_daemon_gracefully
    return "\n".join(
        (ROOT / path).read_text("utf-8")
        for path in (
            "src/hyprial/shell/impl/cli/commands/common/pending_restart.py",
            "src/hyprial/shell/impl/cli/commands/daemon/restart.py",
        )
    )


def test_the_guarantee_is_the_sum_of_what_it_promises_to_cover() -> None:
    """The constant must stay tied to the budgets, not drift beside them."""

    assert TEARDOWN_BUDGETED_SECONDS == _CLOSE_BUDGET_SECONDS + _EXIT_BACKSTOP_SECONDS
    assert _CLOSE_BUDGET_SECONDS == sum(s for _n, s in _CLOSE_STEP_BUDGETS)


def test_duplicate_observation_outlasts_the_teardown_budget() -> None:
    assert DEFAULT_DUPLICATE_OBSERVE_WINDOW == (
        TEARDOWN_BUDGETED_SECONDS + DUPLICATE_OBSERVE_MARGIN_SECONDS
    )
    assert DEFAULT_DUPLICATE_OBSERVE_WINDOW > TEARDOWN_BUDGETED_SECONDS


def test_daemon_close_drains_named_hook_consumers(tmp_path: Path) -> None:
    calls: list[float] = []

    class Consumer:
        def close(self, timeout: float) -> bool:
            calls.append(timeout)
            return True

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="hooks-consumers-close-test",
        hyprial_home=tmp_path / "home",
        owner="owner",
    **_deep_module_daemon_dependencies())
    app._hook_consumers = {"recap": Consumer()}  # type: ignore[assignment]  # noqa: SLF001

    app._close()  # noqa: SLF001

    assert calls == [_TURN_HOOK_CLOSE_TIMEOUT]
    assert app._hook_consumers is None  # noqa: SLF001


def test_daemon_close_stops_autoupdate(tmp_path: Path) -> None:
    # A refactor once moved the attempt(close_autoupdate, ...) call inside the
    # close_autoupdate function itself, so _close never stopped the
    # auto-update authority and every test stayed green.
    calls: list[float] = []

    class Autoupdate:
        stop_incomplete_stage = None

        def stop(self, timeout: float) -> bool:
            calls.append(timeout)
            return True

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="autoupdate-close-test",
        hyprial_home=tmp_path / "home",
        owner="owner",
    **_deep_module_daemon_dependencies())
    real = app._autoupdate  # noqa: SLF001
    app._autoupdate = Autoupdate()  # type: ignore[assignment]  # noqa: SLF001
    try:
        app._close()  # noqa: SLF001
    finally:
        real.stop(timeout=5.0)

    assert calls == [dict(_CLOSE_STEP_BUDGETS)["autoupdate"]]


def test_every_close_budget_in_the_source_is_declared_in_the_guarantee() -> None:
    """⚠️ The check that survives someone adding a step and forgetting this file.

    Reads `_close` and counts the literal second budgets it spends, then
    compares against the declared table. A new `drain(5.0)` that nobody adds
    here would otherwise make every waiter quietly optimistic again -- which
    is exactly the state that produced the outage, since no waiter can notice
    that the thing it waits for grew.

    Deliberately compares the **total**, not the names: matching names would
    pass while a step's budget doubled.
    """

    source = (ROOT / "src/hyprial/daemon/impl/application/shutdown.py").read_text("utf-8")
    tree = ast.parse(source)
    close = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_close"
    )
    found: list[float] = []
    for node in ast.walk(close):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in {"drained", "drain", "shutdown"}:
            continue
        for argument in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, (int, float)
            ):
                found.append(float(argument.value))

    # `drained` appears once but runs per lifecycle domain port, so the source
    # shows one call where several budgets are spent.
    declared_ports = sum(
        1 for name, _s in _CLOSE_STEP_BUDGETS if name.startswith("lifecycle-port:")
    )
    per_port = 5.0
    spent_here = sum(found) + per_port * (declared_ports - 1)

    # ⚠️ Seven declared budgets are deliberately not visible as literals here.
    # `autoupdate` is looked up by name from the table in `_close`, so adding a
    # numeric literal there would defeat the registered-budget contract.
    # `harnesses` is spent by `HarnessRuntime.stop`, whose five seconds is a
    # *default argument* in composition.py -- invisible at the call site in
    # `_close`.  `restore-thread` is spent by `_join_restore_thread`, whose
    # join timeout lives in `_RESTORE_THREAD_JOIN_TIMEOUT` -- a named
    # constant, which this scanner's literal-only reading cannot see.
    # `hooks-consumers` likewise passes its named close timeout. Each invisible
    # budget is checked directly below, because a budget this scan cannot see
    # from the call site is exactly the kind that drifts.
    declared_here = sum(
        seconds
        for name, seconds in _CLOSE_STEP_BUDGETS
        if name
        not in {
            "autoupdate",
            "delivery-hooks",
            "harnesses",
            "hook-bus",
            "restore-thread",
            "hooks-consumers",
            "operator-alarms",
            "org-binding-cache",
            "service-connect",
        }
    )
    assert spent_here == declared_here, (
        f"_close spends {spent_here}s of visible budgets but the table declares "
        f"{declared_here}s for them. A step was added or changed without "
        "updating _CLOSE_STEP_BUDGETS, so every caller waiting on this daemon "
        "would wait less than it may take -- silently, as in 2026-08-31."
    )

    # `operator-alarms`, like `autoupdate`, is looked up by name from the
    # table inside `_close` (no literal to scan). Pin the argument of the
    # exact close call: a nearby unused lookup must not satisfy this guard.
    operator_closes = [
        node
        for node in ast.walk(close)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "operator_alarms"
        and node.func.attr == "close"
    ]
    assert len(operator_closes) == 1, "expected one operator alarm close call"
    operator_close = operator_closes[0]
    timeout_arguments = [
        *operator_close.args,
        *(kw.value for kw in operator_close.keywords if kw.arg == "timeout"),
    ]
    expected_lookup = ast.parse(
        'dict(_CLOSE_STEP_BUDGETS)["operator-alarms"]', mode="eval"
    ).body
    assert len(timeout_arguments) == 1 and ast.dump(
        timeout_arguments[0], include_attributes=False
    ) == ast.dump(expected_lookup, include_attributes=False), (
        "operator alarm close must receive its registered budget directly"
    )
    assert 'dict(_CLOSE_STEP_BUDGETS)["service-connect"]' in source, (
        "_close must spend the registered service-connect budget by name"
    )

    composition = (ROOT / "src/hyprial/daemon/impl/composition/harness_ports.py").read_text("utf-8")
    harness_default = re.search(
        r"def stop\(self, timeout: float = (\d+\.\d+)\)", composition
    )
    assert harness_default, "could not find HarnessRuntime.stop's budget"
    declared_harnesses = dict(_CLOSE_STEP_BUDGETS)["harnesses"]
    assert float(harness_default.group(1)) == declared_harnesses, (
        f"HarnessRuntime.stop budgets {harness_default.group(1)}s while the "
        f"guarantee assumes {declared_harnesses}s"
    )

    # Same checks for the named-constant budgets the literal scanner cannot see.
    assert _RESTORE_THREAD_JOIN_TIMEOUT == dict(_CLOSE_STEP_BUDGETS)[
        "restore-thread"
    ], (
        f"_join_restore_thread waits {_RESTORE_THREAD_JOIN_TIMEOUT}s while "
        f"the guarantee assumes {dict(_CLOSE_STEP_BUDGETS)['restore-thread']}s"
    )
    assert _TURN_HOOK_CLOSE_TIMEOUT == dict(_CLOSE_STEP_BUDGETS)["hooks-consumers"]
    # The delivery intercept port spends the same named close timeout.
    assert _TURN_HOOK_CLOSE_TIMEOUT == dict(_CLOSE_STEP_BUDGETS)["delivery-hooks"]
    assert DISCOVERY_TIMEOUT_SECONDS == dict(_CLOSE_STEP_BUDGETS)[
        "org-binding-cache"
    ]
    assert 'dict(_CLOSE_STEP_BUDGETS)["hook-bus"]' in source
    assert 'dict(_CLOSE_STEP_BUDGETS)["autoupdate"]' in source


def test_every_closing_step_is_classified_as_budgeted_or_not() -> None:
    """The roster, which catches what the sum cannot.

    ⚠️ The two tests in this file look alike and are not:

        sum test    -- a budget that **changed**  (in the table, number drifted)
        roster test -- a step that is **missing** (in no table at all)

    The sum cannot see a step added with no timeout, because such a step adds
    no number. Worse than invisible: it leaves the total looking *more*
    conservative while making it less true, so the failure reads as extra
    safety margin -- and nobody goes back to check a number that erred toward
    caution.

    Membership is therefore compulsory rather than advisory. A new closing
    step belongs to neither table, this goes red, and the author has to decide
    which one it is. Writing *why* a step has no aggregate budget is where they find out
    they do not know; an allow-list would only ask them to copy a name across.

    ⚠️ What this still does not cover, said plainly so its green is not read
    as wider than it is: a blocking step whose budget lives in **another
    file** and carries **no number** is invisible to both tests. Not
    hypothetical -- `harnesses` spends five seconds as a default argument in
    composition.py, and it was caught only because it happened to have a
    number for the sum test to find. The next one may not.
    """

    source = (ROOT / "src/hyprial/daemon/impl/application/shutdown.py").read_text("utf-8")
    tree = ast.parse(source)
    close = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_close"
    )

    steps: set[str] = set()
    for node in ast.walk(close):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) not in {"attempt", "drained"}:
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                steps.add(argument.value)

    assert steps, "no closing steps were found; the scan is looking at nothing"

    classified = {name for name, _ in _CLOSE_STEP_BUDGETS} | {
        name for name, _ in _CLOSE_UNBUDGETED_STEPS
    }
    unclassified = steps - classified
    assert not unclassified, (
        f"closing steps in neither table: {sorted(unclassified)}. Add each to "
        "_CLOSE_STEP_BUDGETS with its timeout, or to _CLOSE_UNBUDGETED_STEPS "
        "with the reason it has no aggregate budget. Leaving one out makes "
        "TEARDOWN_BUDGETED_SECONDS quietly untrue, and a step with no timeout "
        "does not move the sum test at all."
    )

    assert all(
        isinstance(reason, str) and reason.strip()
        for _name, reason in _CLOSE_UNBUDGETED_STEPS
    ), (
        "an unbudgeted step is listed with no reason; the reason is the point "
        "of the list -- without it this is just an allow-list again"
    )


def test_no_waiter_is_shorter_than_the_guarantee() -> None:
    """The inequality the outage was made of, asserted rather than assumed.

    ⚠️ Pinned as a relation, not as values: writing `== 50.0` here would go
    green while a future edit lowered one side and raised the other.
    """

    assert _lock_wait_timeout() >= TEARDOWN_BUDGETED_SECONDS, (
        "a replacement daemon gives up on the lock before the outgoing daemon "
        "has promised to release it"
    )

    cli = (ROOT / "src/hyprial/shell/impl/cli/commands/common/daemon_stop.py").read_text("utf-8")
    # The whole stop path, not a fixed-size prefix: since the supervisor-aware
    # lifecycle (#2526) `_stop_daemon_gracefully` delegates to
    # `_stop_daemon_with_proof`, which owns the receipt deadline.
    def _function(name: str) -> str:
        body = cli[cli.index(f"def {name}(") :]
        return body[: body.index("\ndef ", 1)]

    stop = _function("_stop_daemon_gracefully") + _function("_stop_daemon_with_proof")
    assert "_stop_daemon_with_proof(" in _function("_stop_daemon_gracefully")
    assert "TEARDOWN_BUDGETED_SECONDS" in stop, (
        "the teardown receipt wait is a literal again; it must derive from the "
        "daemon's own guarantee or it will be wrong the next time the guarantee "
        "changes, and nothing will say so"
    )
    assert not re.search(r"deadline = time\.monotonic\(\) \+ \d+\.\d+", stop), (
        "a bare numeric deadline is back in the stop path"
    )


def test_a_stop_that_times_out_does_not_abandon_the_restart() -> None:
    """The half that turned a slow shutdown into an outage.

    `_stop_daemon_gracefully` raising used to propagate out of the upgrade
    path, so `_launch_daemon_process` was never reached. The outgoing daemon
    then exited on its own and nothing was there to replace it.

    🔑 The launcher is the real adjudicator -- it takes the ownership lock and
    fails loudly if the old process is genuinely still holding it. Refusing to
    reach it converts a possible failure into a certain absence.
    """

    cli = _native_restart_source()
    tree = _native_service_call_tree(cli)

    # F5: the skeleton lives in restart_daemon() (pending_restart.py) and the
    # callers route through it; the invariant is per-function now -- a stop
    # call individually guarded inside the same function that launches.
    upgrades = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(inner, ast.Call)
            and getattr(inner.func, "id", None) == "_stop_daemon_gracefully"
            for inner in ast.walk(node)
        )
        and any(
            isinstance(inner, ast.Call)
            and getattr(inner.func, "id", None) == "_launch_daemon_process"
            for inner in ast.walk(node)
        )
    ]
    assert upgrades, "could not find the upgrade restart path"

    for block in upgrades:
        guarded = [
            inner
            for inner in ast.walk(block)
            if isinstance(inner, ast.Try)
            and any(
                isinstance(call, ast.Call)
                and getattr(call.func, "id", None) == "_stop_daemon_gracefully"
                for call in ast.walk(inner)
            )
            and not any(
                isinstance(call, ast.Call)
                and getattr(call.func, "id", None) == "_launch_daemon_process"
                for call in ast.walk(inner)
            )
        ]
        assert guarded, (
            "the stop call is not individually guarded, so a stop timeout "
            "still skips the launch -- the exact control flow that left "
            "production with no daemon on 2026-08-31"
        )


@pytest.mark.parametrize("prefix,binding,call,expected", [
    ("from hyprial.shell.impl.cli.commands.common.services import get_services", "services = get_services()",
     "services._stop_daemon_gracefully()", True),
    ("from unrelated import get_services", "services = get_services()",
     "services._stop_daemon_gracefully()", False),
    ("from hyprial.shell.impl.cli.commands.common.services import get_services", "services = object()",
     "services._stop_daemon_gracefully()", False),
    ("from hyprial.shell.impl.cli.commands.common.services import get_services", "services = get_services(); services = object()",
     "services._stop_daemon_gracefully()", False),
    ("from hyprial.shell.impl.cli.commands.common.services import get_services", "services = get_services()",
     "unrelated._stop_daemon_gracefully()", False),
    ("from hyprial.shell.impl.cli.commands.common.services import get_services; get_services = fake", "services = get_services()",
     "services._stop_daemon_gracefully()", False),
])
def test_native_service_call_binding_rejects_same_name_impersonation(prefix, binding, call, expected):
    tree = _native_service_call_tree(f"{prefix}\ndef operation():\n    {binding}\n    {call}\n")
    recognized = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id == "_stop_daemon_gracefully" for node in ast.walk(tree))
    assert recognized is expected
