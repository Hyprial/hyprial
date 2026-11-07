"""Startup failure events must reconcile with the launch-summary allowlist.

The launch-summary defect had three layers, and the first two fixes left the
third standing: the CLI's ``_SAFE_DAEMON_STARTUP_EVENTS`` was an allowlist of
six success/neutral names, and the daemon's startup failure events
(``daemon.start.failed``, ``harness.recovery.failed``) went only to
``logs/daemon.jsonl`` — a file the failure report path does not read. A daemon
that died binding its IPC socket therefore reported ``daemonEvents: []``,
byte-identical to a daemon that never logged at all.

This module pins both halves of the repair:

1. Producer: the startup failure branches mirror a JSON envelope to stderr —
   the stream the launch capture already merges into the launch log — so the
   summary's existing name-only filter can see them. The envelope carries only
   values the daemon authors itself, and exactly two call sites exist.
2. Reconciliation: every warn/error event the daemon's startup path can emit
   is statically enumerated from ``application.py`` and must be either in the
   CLI allowlist or in the exclusion table below, each exclusion carrying its
   reason. A new ``*.failed`` event added to the startup path without a
   decision turns ``test_startup_event_names_reconciled_with_whitelist`` red
   instead of being silently invisible to the failure report.

The scanner fails rather than skips when it meets a ``_log`` call it cannot
classify: a reconciliation that quietly misses a call shape is the illusion of
completeness, which is how the outage this closes went unseen. Forwarding
lambdas (whose event names live in other modules) are acknowledged one by one
in ``_FORWARDING_SEAMS`` — safe because the consumer side is fail-closed: the
allowlist never admits an event by level, so an unreconciled name can never
reach the summary.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import ast
import inspect
import textwrap
import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessRestoreProjection
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
import hyprial.shell.impl.cli.commands.common.daemon_start as _cli_owner_1

ROOT = Path(__file__).resolve().parents[3]
HYPRIAL = ROOT / ".venv" / "bin" / "hyprial"
def _native_application_source() -> str:
    """Inventory real MRO method definitions; never execute a substitute class."""
    methods: dict[str, str] = {}
    for owner in DaemonApplication.__mro__:
        physical = [method.name for node in ast.parse(inspect.getsource(owner)).body
                    if isinstance(node, ast.ClassDef) for method in node.body
                    if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)] if owner is not object else []
        if len(physical) != len(set(physical)):
            raise RuntimeError(f"Duplicate physical method in {owner.__module__}.{owner.__name__}")
        for name, value in vars(owner).items():
            function = value.__func__ if isinstance(value, staticmethod | classmethod) else value
            if inspect.isfunction(function) and name not in methods:
                methods[name] = inspect.getsource(function)
    if not methods:
        raise RuntimeError("Actual DaemonApplication MRO has no native methods")
    # These are the three exact production Shell callbacks now injected into
    # the former startup body. Follow their physical code, never waive a call.
    from hyprial.shell.impl.daemon_wiring.routines import (
        routine_runtime_factory, routine_template_renderer, routine_text_loader,
    )
    providers = (routine_runtime_factory, routine_template_renderer, routine_text_loader)
    bindings = _deep_module_daemon_dependencies()
    constructor = ast.parse(inspect.getsource(DaemonApplication)).body
    assignments = [node for owner in constructor for node in ast.walk(owner)
                   if isinstance(node, ast.Assign)]
    for provider in providers:
        public = provider.__name__
        private = "_" + public
        if bindings.get(public) is not provider or private in methods:
            raise RuntimeError(f"Noncanonical injected startup provider: {public}")
        if not any(isinstance(node.value, ast.Name) and node.value.id == public
                   and any(isinstance(target, ast.Attribute)
                           and isinstance(target.value, ast.Name) and target.value.id == "self"
                           and target.attr == private for target in node.targets)
                   for node in assignments):
            raise RuntimeError(f"Missing exact constructor port binding: {private}")
        definition = next(node for node in ast.parse(inspect.getsource(provider)).body
                          if isinstance(node, ast.FunctionDef) and node.name == public)
        definition.name = private
        methods[private] = textwrap.indent(ast.unparse(definition), "    ")
    return "class DaemonApplication:\n" + "\n".join(methods.values())


_APPLICATION_SOURCE = _native_application_source()

_ENVELOPE_HELPER = "_mirror_startup_event_to_stderr"

_SERVICE_CONNECT_STEADY_STATE_EVENTS = frozenset(
    {
        "service.connect.registry.updated",
        "service.connect.registry.fallback",
        "service.connect.access.invalid",
        "service.connect.mapping.failed",
        "service.connect.mapping.closed",
        "service.connect.reconcile.completed",
        "service.connect.reconcile.failed",
        "service.connect.restore.failed",
    }
)

# The startup window, per the implementation plan: ``run()`` (including the
# nested ``step()`` wrapper and the failure-path teardown in its ``finally``)
# and the steps it drives. The scanner follows direct ``self._*()`` method
# calls transitively from these roots, so helpers like
# ``_migrate_legacy_channel_pins`` are covered without being named here.
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
# startup window. Each entry is a decision, not an omission.
_STARTUP_BOUNDARIES: dict[str, str] = {
    "_serve": (
        "the accept loop now runs DURING restore (the phase-① boundary); "
        "every IPC handler behind it is dispatch machinery, not a startup "
        "step, and the restore gate (handle()'s DAEMON_RESTORING refusal) "
        "is what the startup window owns there"
    ),
    "_start_ipc_client": (
        "reachable only from _serve; per-connection serving machinery"
    ),
    "_start_maintenance_scheduler": (
        "schedules the reconcile loop, whose first tick fires a second after "
        "ready and then forever; its events are steady-state"
    ),
    "_schedule_maintenance": "see _start_maintenance_scheduler",
    "_scheduled_maintenance": "see _start_maintenance_scheduler",
    "_clock": (
        "injected monotonic clock callable used by SessionRoute expiry; "
        "it is not a DaemonApplication method or startup event emitter"
    ),
}

# Warn/error events the startup path can emit that must NOT join the launch
# summary, with the reason for each. The summary exists to name what failed;
# everything below either describes a non-fatal degradation (startup continues
# past it) or a teardown symptom, and listing it would dilute the one event
# that names the failing step.
_STARTUP_EVENT_EXCLUSIONS: dict[str, str] = {
    "upgrade.steps.pending": (
        "pending explicit upgrade work is an operator notice; the daemon remains "
        "ready and startup continues without running the steps"
    ),
    "identity.org-binding.cache-start-failed": (
        "the org identity-binding cache is additive: its start is wrapped so a "
        "failure never aborts daemon wiring, resolution falls back to the "
        "other sources, and startup continues"
    ),
    "identity.org-binding.publish-start-failed": (
        "publishing this user's binding proof is additive and is retried by the "
        "projector; a failed first attempt does not fail daemon startup"
    ),
    "identity.org-binding.retry-start-failed": (
        "starting the additive binding projector for a deferred or failed "
        "publication did not succeed; daemon startup and org join still continue"
    ),
    "identity.org-binding.publish-failed": (
        "one failed binding-proof publication; the projector retries it and "
        "startup continues"
    ),
    "identity.org-binding.publish-skipped": (
        "no publishable binding proof yet (none stored, or still inside its "
        "publication window); an expected state, not a startup failure"
    ),
    "alias.source-unavailable": (
        "message/PAC/routine input validation degrades one alias lookup to "
        "outside-domain; it is a request-time warning, not a daemon startup failure"
    ),
    "inbox.batch.spill_sweep_failed": "the actor-private inbox-batch spill cache "
    "is bounded cleanup only; failed sweep does not decide messages or stop startup",
    "agent.session_binding.retired_skipped": (
        "a persisted session whose ref was retired by an agent destroy is "
        "deliberately not re-seeded (no binding, no re-created Agent); this is "
        "the expected aftermath of a destroy, not a failed daemon startup"
    ),
    "harness.restore_eligibility_overloaded": (
        "a restore-policy observation could not yet enter the Harness mailbox; "
        "bounded replay and fail-open eligibility handle this degraded state; "
        "the warning itself does not report a failed daemon startup"
    ),
    "restore-pac-facts-degraded": (
        "unavailable PAC restore facts deliberately fail open; startup continues "
        "with degraded eligibility and the warning remains in the daemon log"
    ),
    "session_route.route_retry": (
        "an accepted route mutation retains its exact attempt for reconciliation; "
        "this intermediate retry is not a terminal lifecycle/startup failure"
    ),
    "session_route.route_exhausted": (
        "one route mutation exhausted its bounded retry; the terminal "
        "RouteCommandError goes to the caller that owns the route failure, so "
        "this line adds attempts/deadline detail rather than naming the failing "
        "startup step"
    ),
    "workflow.cutover_cancelled": (
        "one bookkeeping line per legacy run the cutover cancelled, for the "
        "operator and submitter to reconcile from the daemon log; it names no "
        "startup failure (a failed cutover is workflow.cutover_failed)"
    ),
    "network.exposure.skipped": (
        "a persisted exposure that no longer validates is skipped, not fatal; "
        "the operator sees it in `hyprial service exposures` (NOT SERVED) and "
        "in the daemon log, and startup continues"
    ),
    "tailcat.identity-reissued": (
        "the daemon successfully replaced a copied-home Tailcat identity before "
        "starting the forwarding sidecar; this is a recovery observation, not a "
        "startup failure"
    ),
    "config.field.deprecated": (
        "non-fatal configuration hygiene notice; the remedy is in the event's "
        "detail field, and startup continues"
    ),
    "daemon.close.failed": (
        "a teardown step failed while shutting down; the launch report must "
        "name the startup step that failed (daemon.start.failed carries the "
        "phase), not the cleanup noise after it"
    ),
    "daemon.exit.forced": (
        "the exit backstop fired because the process would not leave after "
        "teardown; it names a stall and points at a stack dump, not a startup "
        "failure cause"
    ),
    "daemon.proxy.absent": (
        "a one-line advisory that the daemon has no workerProxy setting and no "
        "proxy in its environment, so model-vendor workers will connect "
        "directly; startup continues and nothing has failed"
    ),
    "daemon.proxy.settings_invalid": (
        "the workerProxy section of settings.json could not be read; startup "
        "continues, and each worker launch that needs the setting fails loudly "
        "with the named error instead"
    ),
    "daemon.interactive_route.not_advertised": (
        "one pre-canonical interactive session keeps receiving but is not "
        "advertised; a per-route downgrade, non-fatal"
    ),
    # U0BRACE: the "harness.bootstrap.rejected" exclusion left with its
    # producer (the construction-phase saga submission deleted as the
    # second owner of non-Lark running rows); nothing emits it anymore.
    "logs.migration_failed": (
        "log archival failed and startup continues; a human-readable WARNING "
        "already goes to stderr from _warn_log_migration_failure"
    ),
    "org.context.publish_failed": (
        "the org-context mesh announcement failed; startup continues and the "
        "next announce retries"
    ),
    "availability_loud.owner_addresses_degraded": (
        "the owner's own requester addresses could not be derived (e.g. an "
        "ambiguous or unreadable users.json); startup continues, the divert "
        "fallback only loses one suppression rule, and every notice still "
        "reaches the daemon log with its reason"
    ),
    "provider.auth.init.failed": (
        "provider-auth alerting could not be wired (e.g. unreadable users.json); "
        "startup continues without auth-failure alerts, and the first real "
        "auth failure still surfaces in worker logs"
    ),
    "pin.migration.conflict": (
        "desired-state channelPin hygiene from a previous era; non-fatal, the "
        "remedy is written in the event detail"
    ),
    "pin.migration.irc-dropped": (
        "notification that a removed feature's pin was dropped; non-fatal"
    ),
    "pin.migration.unknown-adapter": (
        "desired-state channelPin hygiene from a previous era; non-fatal, the "
        "remedy is written in the event detail"
    ),
    "pin.migration.unresolved": (
        "desired-state channelPin hygiene from a previous era; non-fatal, the "
        "remedy is written in the event detail"
    ),
    "routine.coordinator.read_failed": (
        "routine 存储整体读取失败(坏行已由逐行投影隔离,不会到达此处);"
        "daemon 继续启动,但【所有】routine coordinator 均未对账,直到下一次成功读取"
    ),
    "routine.coordinator.reconcile_failed": (
        "one routine coordinator could not be reconciled; the row is isolated "
        "and startup continues to reconcile healthy routines"
    ),
    "zenoh.discovery.failed": (
        "peer discovery is additive by design; failure falls back to the "
        "configured endpoints and startup continues"
    ),
    "zenoh.listen.absent": (
        "partial topology degradation (the node dials out but cannot be "
        "reached); the full-isolation case is already allow-listed as "
        "zenoh.endpoints.unset"
    ),
    # P2 source custody on the boot/restore path. The daemon stays up and
    # keeps serving every other entity; these name the guard's degraded or
    # expected states, never a failed startup step.
    "transfer.custody.unavailable": (
        "one source-custody observation timed out while the daemon classified "
        "restore candidates; that entity stays fail-closed deferred (or is "
        "observed again later) and every other entity still restores"
    ),
    "transfer.restore.suppressed": (
        "the expected outcome of durable transfer custody: a held source is "
        "deliberately not restored, and startup continues for everything else"
    ),
    "transfer.restore.retry_capacity": (
        "the bounded deferred-restore table is full; the entity keeps its "
        "fail-closed suppression and the next restore tick retries, so this is "
        "not a startup failure"
    ),
    "transfer.reconcile.unavailable": (
        "the short P2 journal lane did not answer the first pending-quiesce "
        "read; the durable row stays pending and is retried on the next "
        "restore tick while startup continues"
    ),
    "transfer.reconcile.finish_unavailable": (
        "certifying one already-accepted quiesce receipt against the durable "
        "intent failed; the row stays pending and fail-closed, and startup "
        "continues"
    ),
    "transfer.reconcile.completed": (
        "bookkeeping line for a durable quiesce intent certified against the "
        "lifecycle owner's receipt after a crash; it names a recovered state, "
        "not a startup failure"
    ),
}

# Forwarding lambdas inside the scanned functions pass ``self._log`` to other
# components, so their event names cannot be read from application.py. Each is
# acknowledged here (normalized source segment -> reason). The acknowledgement
# is safe because the consumer is fail-closed: the allowlist admits names, not
# levels, so a failure event routed through one of these seams still cannot
# reach the launch summary unlisted.
_FORWARDING_SEAMS: dict[str, str] = {
    'self._log( "warn", "daemon", notice.code, actor=notice.actor, path=notice.path, message=notice.message, )': (
        "forwards P2 agent runtime-context notices (the one-shot legacy tool "
        "profile migration notice, AGENT_GIT_PROFILE_MIGRATED): "
        "an operator-facing warn line emitted once per actor/code/path after "
        "the legacy directory is renamed aside; never a startup failure"
    ),
    'self._log( level, domain, event, **fields )': (
        "forwards OrgFsDirectoryStore row-hygiene events (directory."
        "device_owner_mismatch / protected_doc_id_mismatch / device_invalid / "
        "binding_invalid / device_key_conflict): one directory row is ignored "
        "and logged, the rest of the directory is served; never a startup "
        "failure"
    ),
    'self._log(level, "identity", event, **fields)': (
        "forwards OrgBindingSource/OrgBindingCacheProjector events "
        "(identity.org-binding.ignored / cache-row-invalid / cache-invalid / "
        "cache-rebuild-failed / verification-unavailable / deferred-publish-"
        "failed): per-row verification drops and background projection "
        "retries; identity resolution degrades to its other sources and startup "
        "continues"
    ),
    'self._log( level, "zenoh", event, **fields )': (
        "forwards ForwardingSidecarSupervisor events (zenoh.forwarding."
        "start_failed / restarting / started / recovered / failed / "
        "exited). Honest about the startup window: the FIRST ensure_started() "
        "runs synchronously on the startup path, and its failure name "
        "zenoh.forwarding.start_failed is already in the launch-summary "
        "allowlist (cli.py) — that is the diagnosis the summary needs. The "
        "rest are the relaunch/recovery loop's progress and steady-state "
        "health observations with their own status/ps surface; they reach "
        "the summary only alongside an already-allowlisted failure name"
    ),
    'self._log( level, "org", event, **fields )': (
        "forwards OrgContextMesh events (org.context.rejected — mesh "
        "announcement hygiene from a zenoh subscription callback); mesh "
        "traffic, not this node's startup outcome"
    ),
    'self._log( level, "orgfs", event, **fields )': (
        "forwards OrgFsMesh events (orgfs.update.rejected and "
        "orgfs.update.blob-fetch-failed) from subscription callbacks; inbound "
        "mesh traffic is steady-state data hygiene, not this node's startup outcome"
    ),
    'self._log( level, "user-delivery", event, **fields )': (
        "forwards receiver-owned delivery path events (user-delivery.proxy / "
        "user-delivery.squire) emitted only when a steady-state user message "
        "is handled; wiring the callback during startup emits nothing"
    ),
    "self._log( level, component, event, **fields )": (
        "forwards DaemonEventBridge reconcile-loop events "
        "(harness.delivery.*, harness.session_ref.persist_failed); "
        "DaemonEventBridge.start() itself never logs through this seam"
    ),
    'self._log( level, "daemon", event, **fields )': (
        "forwards _HarnessActorRegistration lifecycle events "
        "(harness.actor.registration.closed/invalidated); close-with-reason "
        "is info, and the warn variants are steady-state health observations"
    ),
}

# The complete set of stderr-envelope producer call sites: (enclosing
# DaemonApplication method, literal event name). The risk this pins: a daemon
# that talks on stderr on its normal path turns the launch log into a new
# noise source, so the helper may be called from exactly these two failure
# branches.
_ENVELOPE_PRODUCERS = frozenset(
    {
        ("run", "daemon.start.failed"),
        ("_restore_harnesses", "harness.recovery.failed"),
    }
)

_LEVELS_QUIET = frozenset({"debug", "info"})
_LEVELS_LOUD = frozenset({"warn", "error"})


@dataclass(frozen=True)
class _StartupEventScan:
    """One static reading of the daemon startup path."""

    # (event, method, lineno) for every warn/error ``_log``/``_log_trace``
    # call with literal level and event.
    event_sites: tuple[tuple[str, str, int], ...]
    # Normalized source of every ``_log*`` call the scanner could not
    # classify (non-literal level or event). Never silently skipped.
    unclassifiable: frozenset[str]
    # Direct ``self._*()`` calls that resolve to no method and no boundary.
    unresolved: tuple[str, ...]
    # (method, event) call sites of the stderr-envelope helper.
    envelope_producers: frozenset[tuple[str, str]]

    @property
    def events(self) -> frozenset[str]:
        return frozenset(event for event, _, _ in self.event_sites)


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _scan_startup_events(source: str) -> _StartupEventScan:
    tree = ast.parse(source, filename="hyprial/daemon/application.py")
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DaemonApplication":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name] = item

    event_sites: dict[str, tuple[str, str, int]] = {}
    unclassifiable: set[str] = set()
    unresolved: list[str] = []
    producers: set[tuple[str, str]] = set()
    visited: set[str] = set()
    stack = sorted(_STARTUP_ROOTS)
    while stack:
        name = stack.pop()
        if name in visited or name in _STARTUP_BOUNDARIES:
            continue
        visited.add(name)
        function = methods.get(name)
        if function is None:
            unresolved.append(
                f"startup root {name}() is not a DaemonApplication method; "
                "if it was renamed, rename the root in this test too"
            )
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == _ENVELOPE_HELPER:
                event = _literal(node.args[0]) if node.args else None
                if event is None:
                    unclassifiable.add(
                        f"{_ENVELOPE_HELPER}(...) at {name}:{node.lineno} "
                        "with a non-literal event"
                    )
                else:
                    producers.add((name, event))
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                continue
            attr = func.attr
            if attr in ("_log", "_log_trace"):
                level = _literal(node.args[0]) if node.args else None
                event_index = 2 if attr == "_log" else 1
                event = (
                    _literal(node.args[event_index])
                    if len(node.args) > event_index
                    else None
                )
                if level in _LEVELS_QUIET:
                    continue
                if level in _LEVELS_LOUD and event is not None:
                    event_sites.setdefault(event, (event, name, node.lineno))
                    continue
                segment = ast.get_source_segment(source, node)
                unclassifiable.add(
                    " ".join(segment.split())
                    if segment is not None
                    else f"{attr} call at {name}:{node.lineno}"
                )
            elif attr.startswith("_"):
                if attr in methods:
                    stack.append(attr)
                elif attr not in _STARTUP_BOUNDARIES:
                    unresolved.append(
                        f"self.{attr}() called from {name}:{node.lineno} is "
                        "neither a DaemonApplication method nor a declared "
                        "boundary; the scanner follows only direct method "
                        "calls, so decide which it is"
                    )
    return _StartupEventScan(
        event_sites=tuple(sorted(event_sites.values())),
        unclassifiable=frozenset(unclassifiable),
        unresolved=tuple(unresolved),
        envelope_producers=frozenset(producers),
    )


def _reconciliation_problems(scan: _StartupEventScan) -> list[str]:
    problems: list[str] = []
    problems.extend(scan.unresolved)
    allowlist = set(_cli_owner_1._SAFE_DAEMON_STARTUP_EVENTS)
    exclusions = set(_STARTUP_EVENT_EXCLUSIONS)
    unreconciled = scan.events - allowlist - exclusions
    for event in sorted(unreconciled):
        sites = ", ".join(
            f"{method}:{lineno}"
            for name, method, lineno in scan.event_sites
            if name == event
        )
        problems.append(
            f"{event!r} ({sites}) can be emitted on the daemon startup path "
            "but is neither in cli._SAFE_DAEMON_STARTUP_EVENTS nor in "
            "_STARTUP_EVENT_EXCLUSIONS; decide which. If it names a startup "
            "failure it belongs in the allowlist — the launch summary is the "
            "only place a failed `hyprial init` names its cause"
        )
    for event in sorted(exclusions - scan.events):
        problems.append(
            f"exclusion {event!r} no longer appears on the startup path; "
            "delete the row — a stale table is how the next event gets "
            "excused by accident"
        )
    for seam in sorted(scan.unclassifiable - set(_FORWARDING_SEAMS)):
        problems.append(
            f"unclassifiable _log call {seam!r}; the scanner fails rather "
            "than skips, so either give the call a literal level and event "
            "or acknowledge the forwarding seam in _FORWARDING_SEAMS with "
            "its reason"
        )
    for seam in sorted(set(_FORWARDING_SEAMS) - scan.unclassifiable):
        problems.append(
            f"acknowledged forwarding seam {seam!r} is gone from the "
            "startup path; remove its row from _FORWARDING_SEAMS"
        )
    if scan.envelope_producers != _ENVELOPE_PRODUCERS:
        problems.append(
            f"{_ENVELOPE_HELPER} call sites are "
            f"{sorted(scan.envelope_producers)}, expected "
            f"{sorted(_ENVELOPE_PRODUCERS)}; stderr envelopes exist for "
            "startup failures only — a daemon that talks on stderr on its "
            "normal path turns the launch log into noise"
        )
    for _method, event in sorted(_ENVELOPE_PRODUCERS):
        if event not in allowlist:
            problems.append(
                f"{event!r} is mirrored to the launch log but missing from "
                "cli._SAFE_DAEMON_STARTUP_EVENTS — the producer/consumer "
                "pair is disconnected"
            )
    return problems


def test_startup_event_names_reconciled_with_whitelist() -> None:
    scan = _scan_startup_events(_APPLICATION_SOURCE)
    problems = _reconciliation_problems(scan)
    assert not problems, "\n".join(problems)


def test_service_connect_events_are_classified_as_post_readiness() -> None:
    """Service restore/reconcile is scheduled after readiness, never summarized
    as a synchronous daemon-start failure.

    Pin the frozen contract namespace here so a new service event must receive
    an explicit startup-versus-steady-state decision instead of silently
    bypassing the startup scanner.
    """

    contract = (ROOT / "contract/service-connect/README.md").read_text("utf-8")
    declared = {
        line.removeprefix("- `").removesuffix("`")
        for line in contract.splitlines()
        if line.startswith("- `service.connect.") and line.endswith("`")
    }
    assert declared == _SERVICE_CONNECT_STEADY_STATE_EVENTS
    assert _SERVICE_CONNECT_STEADY_STATE_EVENTS.isdisjoint(
        _cli_owner_1._SAFE_DAEMON_STARTUP_EVENTS
    )
    assert _SERVICE_CONNECT_STEADY_STATE_EVENTS.isdisjoint(
        _scan_startup_events(_APPLICATION_SOURCE).events
    )


def test_the_scanner_goes_red_on_an_unreconciled_event() -> None:
    """The reconciliation's self-check: an unlisted new event must fail it.

    A reconciliation that cannot be made red by the change it exists to catch
    is decoration. Inject a warn event into ``run()`` — outside both tables —
    and the verdict must name it.
    """

    anchor = "            step(self._write_pid_file, DaemonStartupPhase.PID_FILE)"
    assert anchor in _APPLICATION_SOURCE
    mutated = _APPLICATION_SOURCE.replace(
        anchor,
        anchor + '\n            self._log("warn", "daemon", "test.unreconciled.failed")',
        1,
    )
    scan = _scan_startup_events(mutated)
    assert "test.unreconciled.failed" in scan.events
    problems = _reconciliation_problems(scan)
    assert any("test.unreconciled.failed" in problem for problem in problems), (
        "an unlisted startup-path event produced no reconciliation problem: "
        f"{problems}"
    )


def test_the_scanner_goes_red_on_an_unclassifiable_log_call() -> None:
    """Fail-don't-skip: a non-literal event must not pass silently."""

    anchor = "            step(self._write_pid_file, DaemonStartupPhase.PID_FILE)"
    assert anchor in _APPLICATION_SOURCE
    mutated = _APPLICATION_SOURCE.replace(
        anchor,
        anchor + '\n            self._log("warn", "daemon", event_name)',
        1,
    )
    scan = _scan_startup_events(mutated)
    problems = _reconciliation_problems(scan)
    assert any("unclassifiable" in problem for problem in problems), (
        "a _log call the scanner cannot classify produced no problem: "
        f"{problems}"
    )


# --------------------------------------------------------------------------- #
# The producer half: startup failure branches mirror one JSON envelope to
# stderr, which the launch capture already merges into the launch log.
# --------------------------------------------------------------------------- #

NODE = "startup-event-node"
OWNER = "tester"


def _application(
    tmp_path: Path,
    *,
    socket_path: Path | None = None,
    desired: list[dict[str, Any]] | None = None,
) -> DaemonApplication:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state_dir = home / "state"
    state_dir.mkdir(parents=True)
    if desired is not None:
        # The seed must exist BEFORE the store constructs: construction is
        # what runs the .v1 -> SQLite migration (U0a-2).
        _write_desired(state_dir, desired)
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=socket_path or state_dir / "daemon.sock",
        node_id=NODE,
        hyprial_home=home,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    _TEST_APPLICATIONS.append(app)
    return app


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_test_applications():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        app = _TEST_APPLICATIONS.pop()
        if not app._agent_session_domains._domains_closed:
            app._close()
        app._close_home_lease()


def _write_desired(state_dir: Path, providers: list[dict[str, Any]]) -> None:
    desired = state_dir / "desired-state.json"
    desired.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{desired}.v1").write_text(
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


def _provider(harness: str, name: str) -> dict[str, Any]:
    return {
        "provider": harness,
        "name": name,
        "headless": True,
        "args": [],
        "ownership": "managed",
    }


class _RestoringHarnesses:
    def restore(self, desired: tuple[Any, ...]) -> HarnessRestoreProjection:
        # The real summary type, not a fabricated shape: restore's contract
        # is HarnessRestoreProjection (F1 added the ``deferred`` count) and
        # the daemon's recovery log reads its fields.
        return HarnessRestoreProjection(
            attempted=len(desired), restored=len(desired), failed=0, deferred=0
        )


class _CollapsingHarnesses:
    def restore(self, desired: tuple[Any, ...]) -> HarnessRestoreProjection:
        raise RuntimeError("synthetic restore collapse")


def _stderr_envelopes(capsys: Any) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    for line in capsys.readouterr().err.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("event"), str):
            envelopes.append(entry)
    return envelopes


def test_failed_step_mirrors_exactly_one_stderr_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """The ipc-server failure shape from the original incident, made visible.

    A socket path beyond the AF_UNIX sun_path limit (104 bytes on macOS, 108
    on Linux) fails ``bind`` on every platform. Before the envelope, this
    startup died with ``daemonEvents: []``.
    """

    deep = tmp_path / ("d" * 120) / "state"
    deep.mkdir(parents=True)
    app = _application(tmp_path, socket_path=deep / "daemon.sock")
    monkeypatch.setattr(app, "_start_runtime", lambda: None)

    with pytest.raises(OSError):
        app.run()

    assert _stderr_envelopes(capsys) == [
        {"event": "daemon.start.failed", "phase": "ipc-server"}
    ]


def test_service_connect_construction_failure_is_a_named_startup_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    app = _application(tmp_path)
    access = app.hyprial_home / "state" / "tailcat" / "service-access.json"
    access.parent.mkdir(mode=0o700)
    access.write_text("{}", encoding="utf-8")
    access.chmod(0o600)
    monkeypatch.setattr(app, "_start_runtime", lambda: None)
    monkeypatch.setattr(
        app,
        "_require_orgfs_runtime",
        lambda: (_ for _ in ()).throw(TypeError("synthetic state.load mismatch")),
    )

    with pytest.raises(TypeError, match="synthetic state.load mismatch"):
        app.run()

    assert _stderr_envelopes(capsys) == [
        {"event": "daemon.start.failed", "phase": "service-connect"}
    ]


def test_restore_failure_mirrors_envelope_and_keeps_detail_in_the_structured_log(
    tmp_path: Path, capsys: Any
) -> None:
    app = _application(tmp_path, desired=[_provider("pi", "squire")])
    app._harnesses = _CollapsingHarnesses()  # type: ignore[assignment]

    # Must not raise: the daemon outlives one connector's restore.
    app._restore_harnesses()

    envelopes = _stderr_envelopes(capsys)
    assert envelopes == [{"event": "harness.recovery.failed", "declared": 1}]
    # The envelope carries only daemon-authored values; the exception text
    # (which can drag in external bytes) stays in the redacted JSONL copy.
    assert "synthetic restore collapse" not in json.dumps(envelopes)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = app._logger.writer_status()
        if status is None or status.written + status.failed >= status.accepted:
            break
        time.sleep(0.005)
    records = [
        json.loads(line)
        for line in (app.state_dir / "logs" / "daemon.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    warnings = [r for r in records if r.get("event") == "harness.recovery.failed"]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "warn"
    assert "synthetic restore collapse" in warnings[0]["error"]


def test_successful_restore_writes_no_stderr_envelope(
    tmp_path: Path, capsys: Any
) -> None:
    app = _application(tmp_path, desired=[_provider("pi", "squire")])
    app._harnesses = _RestoringHarnesses()  # type: ignore[assignment]

    app._restore_harnesses()

    assert _stderr_envelopes(capsys) == []


# --------------------------------------------------------------------------- #
# The consumer half: the summary takes event names, never envelope fields.
# --------------------------------------------------------------------------- #


def test_launch_summary_takes_failure_event_names_but_never_envelope_fields(
    tmp_path: Path,
) -> None:
    stream, capture, marker, offset = _cli_owner_1._daemon_launch_capture(tmp_path)
    try:
        stream.write(
            b'{"event":"daemon.start.failed","phase":"ipc-server",'
            b'"error":"<attacker-bytes>"}\n'
            b'{"event":"harness.recovery.failed","declared":3,'
            b'"error":"<attacker-two>"}\n'
        )
        summary = _cli_owner_1._daemon_launch_log_summary(stream, marker=marker, offset=offset)
        assert summary["daemonEvents"] == [
            "daemon.start.failed",
            "harness.recovery.failed",
        ]
        serialized = json.dumps(summary)
        assert "<attacker-bytes>" not in serialized
        assert "<attacker-two>" not in serialized
        assert "phase" not in serialized
    finally:
        stream.close()
        capture.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# End to end: a daemon that dies at ipc-server bind surfaces the failure
# event in the `hyprial init` report — the exact report shape the original
# incident should have produced.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HYPRIAL.exists(), reason="editable hyprial executable is unavailable")
def test_failed_startup_surfaces_event_in_launch_summary() -> None:
    # ``/private/tmp`` is macOS-specific; the portable short alias is /tmp.
    with tempfile.TemporaryDirectory(prefix="hyprial-u5-", dir=hyprial_test_tmp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        # The state root puts daemon.sock beyond the AF_UNIX sun_path limit
        # on both macOS (104) and Linux (108), so the child daemon dies at
        # the ipc-server step deterministically.
        state = root / ("s" * 120) / "state"
        from tests.support.runtime.environment import isolated_environment

        env = isolated_environment(root)
        env.update(
            {
                "HYPRIAL_HOME": str(home),
                "HARNESS_STATE_DIR": str(state),
                "HYPRIAL_NODE_ID": "startup-event-e2e",
                # Never inherit a developer shell's production zenoh endpoints.
                "HYPRIAL_ZENOH_LISTEN": "",
                "HYPRIAL_ZENOH_CONNECT": "",
                "HYPRIAL_PEER_DISCOVERY": "0",
            }
        )
        env.pop("HARNESS_SOCKET_PATH", None)
        env.pop("HYPRIAL_INIT_READY_TIMEOUT", None)

        completed = subprocess.run(
            # ``--no-service``: the daemon is expected to FAIL startup here; the
            # per-user supervisor is boot-persistent and would keep relaunching
            # it for a home this test deletes.
            [str(HYPRIAL), "init", "--no-service", "--ready-timeout", "30", "--json"],
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )

        assert completed.returncode == 1, (completed.stdout, completed.stderr)
        payload = json.loads(completed.stdout)
        assert payload["code"] == "DAEMON_START_FAILED"
        data = payload["data"]
        assert data["exitCode"] != 0, payload
        assert payload["error"] == (
            f"daemon exited during startup with status {data['exitCode']}"
        )
        assert data["phase"] == "startup"
        assert data["logAvailable"] is True
        assert "daemon.start.failed" in data["daemonEvents"]

        records = [
            json.loads(line)
            for line in (state / "logs" / "daemon.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        failures = [r for r in records if r.get("event") == "daemon.start.failed"]
        assert len(failures) == 1
        assert failures[0]["phase"] == "ipc-server"
        assert failures[0]["errorType"] == "OSError"
        assert "AF_UNIX path too long" in failures[0]["error"]
        endpoints = [
            r for r in records if r.get("event") == "zenoh.endpoints.resolved"
        ]
        assert len(endpoints) == 1
        assert endpoints[0]["discovery"] == "disabled"
        assert endpoints[0]["discovered"] == 0
        assert endpoints[0]["effective"] == 0
