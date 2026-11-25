"""Production local member facts and non-blocking harness effect adapter (I6-2).

Decision notes (cell model slice I6-2, plan §F, §H.1-H.3):

- ``AppLocalMemberFacts`` resolves ``(agent_id, member)`` launch facts from
  the app's in-memory agent directory (for external-custody guards and actor
  name lookup) and ``desired_state`` without touching OrgFS or spawning I/O.
- ``AppMemberEffectPort`` dispatches ``cell.member.ensure``,
  ``cell.member.stop`` and ``cell.member.policy.apply`` non-blockingly on a
  bounded executor, journaling each fenced intent in ``_LifecycleStore`` (when
  ``app.state_db`` is present), driving ``HarnessPortClient`` /
  ``HarnessRuntimeActor`` off the cell reconcile actor thread, and reporting
  live-verified digests back to ``CellReconcileCoordinator.complete_effect``.
- I6-5a: the member policy reaches the launch through the app's standing
  ``MemberLaunchPolicies`` lookup (``app._cell_member_policies``), which the
  worker-channel factory reads.  A spec with a policy is verified against
  the worker's ready-frame acknowledgement before its digest is reported.
  ``cell.member.policy.apply`` stops the member, launches the new policy and
  verifies it; on failure it relaunches the last verified policy and
  reports the previous spec's digest with the failure, so the actor keeps
  the last effective policy.
- An ensure waits for readiness under the harness actor's own configured
  start budget (``harnesses.actor.start_timeout``, the manifested start
  supervision of the harness actor) instead of a second, private readiness
  limit: the member is not judged unready before the actor that owns the
  start would itself give up on it.  The production daemon always runs the
  harness actor with a positive start budget; a runtime configured with no
  start timer (``0``) gets no readiness wait here either, so its ensures
  report ``MEMBER_NOT_READY`` unless the member is already ready.
- The model vendor of a launch spec is read from its wire JSON key
  ``modelProvider``, the same route the desired-state SQLite store takes,
  so this module never names the reserved terminology attribute.
- I6-5a (last step): ``member_launch_policy`` is the one place every harness
  launch gets its member policy (the worker-channel factory calls it for
  ``lifecycle.start``, restore, crash restart and reconcile alike).  A policy
  reconcile published for the launch key wins; otherwise the member's rules
  are read from the local cell cache file (``read_cached_cell``, read-only, no
  OrgFS, no composition).  It fails closed: no agent, an unreadable cache, a
  cell of another authority, a document with problems or a harness without a
  mapping all give no policy, logged, and the worker gets channel wiring only.
  It never invents a policy.  A launch spec whose member is unset or the
  agent's own name is the agent's one harness member, the record's URI (the
  inverse of ``_matching_desired_spec``).
- I6-5b (Q0-e, deviates from ruling §12.1.3 "审计保留"): apply outcomes are
  cell observability events on the daemon log, not a separate JSONL audit
  store.  ``cell.member_policy_apply_started`` / ``cell.member_policy_applied``
  / ``cell.member_policy_apply_failed`` carry the cell, member, harness,
  revision (the policy version) and digests; a failure says whether the last
  verified policy was relaunched (``rolledBack``).  A first launch whose
  policy was acknowledged emits ``cell.member_policy_applied`` with
  ``action=ensure``.  Logging never changes an outcome.
- Critic P1-2: publishing "no policy" is an entry; ``member_launch_policy``
  honours it without reading the document, so a rollback to a policy-less
  spec never relaunches the rules that just failed.
- Ensure is idempotent (I6-2 restore, ownership and spec fixes, 2026-11-23).
  It reads the live harness under the launch member first and adopts any
  running or starting harness, so a member daemon restore just recovered
  is not restarted; it starts only a missing member.  Every ensure reports
  the live digest, computed over the same normalised launch as the desired
  digest (``member_rows``), and marks an adoption ``adopted``.  An adopted
  member whose live digest differs is logged as
  ``cell.reconcile.member.adopted_with_difference`` and the reconcile actor
  drain-and-restarts it through its journaled stop and ensure: for members
  declared in ``cell.members`` the document is authoritative.  The drift
  restart is bounded to once per member: the adapter remembers a member it
  stopped for ``adopted-spec-drift``, and if the following ensure adopts a
  live process that still differs it reports the adoption as not adopted,
  so the actor fails the member with ``config-digest-mismatch`` instead of
  stopping a live agent again.  The latch clears on any ensure that settles
  on the desired digest.  The launch member is the canonical I6-1B
  ``member_name`` of the matching local launch, and a member name that
  cannot form a member actor URI fails the effect with the registered
  ``HARNESS_START_FAILED`` code before any harness call.
- A reconcile start launches the full desired-state row (I6-2 spec fix),
  not a reduced copy of the digest fields: the row is read at execution
  time, its args are normalised, and only the document-driven fields and
  the digest fields of the spec are replaced, so ``session_ref``,
  ``containerized``, ``nickname`` and every other row field survive.
  ``to_launch_spec`` is used only when no row exists.  After a fresh start
  that settles on the desired digest, a document-driven model change is
  written back to the row (re-read right before the write, only ``model``
  changes).  For a member that is not the agent's own, a document harness
  kind that differs from the row's is launched but not written back; ensure
  also adopts a live harness of the row's kind, so its live digest differs
  and the bounded drift restart stops it before the document kind starts
  (one member never runs two kinds; the new kind launches without the row's
  ``session_ref``).  The agent's own member refuses a kind change instead
  (``own_member/effects.py``).  Accepted limits are KP-122.
- I6-5 merged onto I6-2 (2026-11-24): the policy is one more input of the
  config digest, so an adopted process and a configuration change restart
  through the same STOP-then-ENSURE chain.  The one exception is a
  policy-only change on a verified running member, which the planner sends
  as its own ``POLICY_APPLY`` effect (stop and start inside it, rollback to
  the last verified policy; integration critic P2-6).  The live digest of an
  adopted member uses the policy digest the process acknowledged in its
  ready frame, so an adopted process that never acknowledged the desired
  policy reads as differing and takes the bounded drift restart; a fresh
  start publishes the policy first and verifies the acknowledgement before
  it reports.  Policy keys are ``(harness, actor, launch member)``, the
  ``launch_key`` of the spec actually launched.  The agent's own member
  joins the chain too: its ENSURE adopts, reports drift, or restarts in
  place only as the restart half after a restart STOP (see
  ``own_member/effects.py``).  ``nudge_member_reconcile`` tells the
  coordinator nothing while the startup barrier is closed.
- Which members reconcile owns at all is decided by the planner (only
  members declared in ``cell.members``); see ``cells/reconcile/planner.py``.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.reconcile import (
    CUSTODY_DAEMON,
    CUSTODY_EXTERNAL,
    CellReconcileCoordinator,
    DesiredMemberSpec,
    LocalMemberFactsPort,
    LocalMemberLaunchFacts,
    MemberEffectAction,
    MemberEffectCompletion,
    MemberEffectIntent,
    MemberEffectPort,
)
from hyprial.daemon.impl.cells.reconcile.policy import (
    LaunchPolicy,
    MemberLaunchPolicies,
    PolicyHandshakeError,
    PolicyKey,
    document_launch_policy,
    require_policy_ack,
)
from hyprial.daemon.impl.cells.member_launch import member_name
from hyprial.daemon.impl.cells.runtime import CACHE_FILENAME, read_cached_cell
from hyprial.daemon.impl.cells.runtime.agents import agent_custody
from hyprial.daemon.impl.composition.cells.member_rows import (
    CellReconcileStartBarrier,
    _agent_for_id,
    _desired_row,
    _log,
    _matching_desired_spec,
    _model_vendor,
    _restore_launch_row,
    live_member_digest,
)
from hyprial.daemon.impl.composition.cells.own_member import (
    POLICY_APPLIED_EVENT,
    POLICY_APPLY_FAILED_EVENT,
    POLICY_APPLY_STARTED_EVENT,
    POLICY_UNAVAILABLE_EVENT,
    OwnMemberEffects,
)
from hyprial.daemon.impl.lifecycle_manager import _LifecycleStore
from hyprial.error.codes import HARNESS_START_FAILED
from hyprial.kernel import member_actor_uri


def _log_policy(app: Any, level: str, spec: Any, reason: str, **fields: Any) -> None:
    log = getattr(app, "_log", None)
    if not callable(log):
        return
    try:
        log(level, "cells", POLICY_UNAVAILABLE_EVENT,
            harness=getattr(spec, "harness", None), name=getattr(spec, "name", None),
            reason=reason, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks a launch
        pass


def _agent_named(app: Any, name: str) -> Any:
    agents = getattr(app, "agents", None)
    get = getattr(agents, "get", None)
    if callable(get):
        return get(name)
    listing = getattr(agents, "list", None)
    if not callable(listing):
        return None
    return next((item for item in listing() if getattr(item, "actor", None) == name), None)


def launch_member_name(agent: Any, spec: Any) -> str:
    """The cell member a launch spec runs: an explicit member, else the agent's URI."""

    member = getattr(spec, "member", None)
    own = {getattr(spec, "name", None), getattr(agent, "actor", None)}
    if isinstance(member, str) and member and member not in own:
        return member
    return str(agent.uri)


def member_launch_policy(app: Any, spec: Any) -> LaunchPolicy | None:
    """The member policy a harness launch of *spec* receives, or None.

    The answer is recorded as what that launch ran, so a failed restart can
    relaunch it.
    """

    launch = _resolve_launch_policy(app, spec)
    policies = getattr(app, "_cell_member_policies", None)
    if isinstance(policies, MemberLaunchPolicies):
        policies.note_launch(spec, launch)
    return launch


def _resolve_launch_policy(app: Any, spec: Any) -> LaunchPolicy | None:
    policies = getattr(app, "_cell_member_policies", None)
    if isinstance(policies, MemberLaunchPolicies):
        published, launch = policies.lookup(spec)
        if published:
            return launch
    harness = getattr(spec, "harness", None)
    if not isinstance(harness, str) or not harness:
        return None
    try:
        agent = _agent_named(app, spec.name)
    except Exception as error:  # noqa: BLE001 - fail closed without a record
        _log_policy(app, "warn", spec, "agent-unreadable", errorType=type(error).__name__)
        return None
    if agent is None:
        _log_policy(app, "info", spec, "no-agent-record")
        return None
    if agent_custody(agent) == CUSTODY_EXTERNAL:
        return None
    try:
        from hyprial.daemon.impl.composition.cells.wiring import local_cell_authority

        authority = local_cell_authority(app)
        cell = read_cached_cell(Path(app.state_dir) / CACHE_FILENAME, agent.cell_id)
    except Exception as error:  # noqa: BLE001 - an unreadable index never invents a policy
        _log_policy(app, "warn", spec, "cell-index-unavailable",
                    errorType=type(error).__name__, detail=str(error)[:200])
        return None
    if cell is None:
        _log_policy(app, "info", spec, "no-cell-document")
        return None
    if cell.authority != authority:
        _log_policy(app, "warn", spec, "cell-authority-mismatch")
        return None
    launch, reason = document_launch_policy(cell, launch_member_name(agent, spec), harness)
    if reason is not None:
        _log_policy(app, "warn", spec, reason, revision=cell.revision)
    return launch


def nudge_member_reconcile(app: Any, actor: str) -> None:
    """Re-run reconcile for *actor*'s agent cell after a launch; never composes or raises."""

    runtime = getattr(app, "_cell_runtime", None)
    coordinator = getattr(app, "_cell_reconcile_coordinator", None)
    if runtime is None or coordinator is None:
        return
    barrier = getattr(app, "_cell_reconcile_barrier", None)
    if isinstance(barrier, CellReconcileStartBarrier) and not barrier.is_open:
        return
    try:
        agent = _agent_named(app, actor)
        cell = None if agent is None else runtime.lookup(agent.cell_id)
        if cell is not None:
            in_progress = getattr(
                getattr(app, "_cell_reconcile_effects", None), "own_launch_in_progress", None
            )
            by_reconcile = callable(in_progress) and in_progress(actor)
            coordinator.on_index((cell,), recheck=() if by_reconcile else (str(agent.uri),))
    except Exception as error:  # noqa: BLE001 - a nudge never fails the launch
        log = getattr(app, "_log", None)
        if callable(log):
            try:
                log("warn", "cells", "cell.member_reconcile_nudge_failed", actor=actor,
                    errorType=type(error).__name__)
            except Exception:  # noqa: BLE001 - logging never breaks a launch
                pass


class AppLocalMemberFacts(LocalMemberFactsPort):
    """Read-only ``LocalMemberFactsPort`` backed by ``app.agents`` and ``app.desired_state``."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def facts_for(self, agent_id: str, member: str) -> LocalMemberLaunchFacts | None:
        agent = _agent_for_id(self._app, agent_id)
        if agent is None:
            return None
        if agent_custody(agent) == CUSTODY_EXTERNAL:
            return LocalMemberLaunchFacts(
                agent_id=agent_id,
                member=member,
                custody=CUSTODY_EXTERNAL,
            )
        desired_state = getattr(self._app, "desired_state", None)
        loader = getattr(desired_state, "load", None)
        if not callable(loader):
            return None
        try:
            harnesses = tuple(loader().harnesses)
        except Exception:  # noqa: BLE001 - unreadable desired state degrades affected member
            return None
        row = _matching_desired_spec(harnesses, agent, member)
        if row is None:
            return None
        spec = _restore_launch_row(self._app, row)
        args = tuple(spec.args)
        approval = "approved" if "--approve" in args else None
        return LocalMemberLaunchFacts(
            agent_id=agent_id,
            member=member,
            harness=getattr(spec, "harness", None),
            cwd=getattr(spec, "cwd", None),
            command=tuple(getattr(spec, "command", ()) or ()),
            args=args,
            model_vendor=_model_vendor(spec),
            model=getattr(spec, "model", None),
            approval=approval,
            custody=CUSTODY_DAEMON,
            launch_member=member_name(spec),
            held=getattr(spec, "status", "running") != "running",
        )


class AppMemberEffectPort(OwnMemberEffects, MemberEffectPort):
    """Non-blocking ``MemberEffectPort`` driving ``HarnessPortClient`` and ``_LifecycleStore``."""

    def __init__(
        self,
        app: Any,
        *,
        facts_port: LocalMemberFactsPort,
        max_workers: int = 4,
    ) -> None:
        self._app = app
        self._facts_port = facts_port
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="hyprial-cell-reconcile",
        )
        self._lock = threading.Lock()
        self._closed = False
        self._coordinator: CellReconcileCoordinator | None = None
        self._started_specs: dict[tuple[str, str], DesiredMemberSpec] = {}
        self._own_specs: dict[tuple[str, str], Any] = {}
        self._own_launching: set[str] = set()
        self._drift_stopped: set[tuple[str, str]] = set()
        self._drift_exhausted: dict[tuple[str, str], str] = {}
        self._ordinal_counter = 0

    def bind_coordinator(self, coordinator: CellReconcileCoordinator) -> None:
        with self._lock:
            self._coordinator = coordinator

    def submit(self, intent: MemberEffectIntent) -> None:
        with self._lock:
            if self._closed:
                return
            if intent.spec is not None and intent.action is not MemberEffectAction.STOP:
                self._started_specs[(intent.agent_id, intent.fence.member)] = intent.spec
            self._ordinal_counter += 1
            ordinal = self._ordinal_counter
        self._executor.submit(self._run_intent, intent, ordinal)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run_intent(self, intent: MemberEffectIntent, ordinal: int) -> None:
        correlation_id = f"cell-reconcile:{intent.attempt_id}:{uuid.uuid4().hex[:8]}"
        store = self._lifecycle_store()
        if store is not None:
            try:
                store.record_cell_member_effect(
                    cell_id=intent.fence.cell_id,
                    epoch=intent.fence.epoch,
                    revision=intent.fence.revision,
                    member=intent.fence.member,
                    effect_name=intent.action.value,
                    correlation_id=correlation_id,
                    attempt_token=intent.attempt_id,
                    ordinal=ordinal,
                )
            except Exception as error:  # noqa: BLE001 - journal failure reports failed completion
                self._deliver_completion(
                    MemberEffectCompletion(
                        fence=intent.fence,
                        attempt_id=intent.attempt_id,
                        action=intent.action,
                        succeeded=False,
                        error_code=type(error).__name__,
                        error_detail=str(error)[:300],
                    )
                )
                return

        try:
            completion = self._execute_intent(intent)
        except Exception as error:  # noqa: BLE001 - harness errors map to failed completion
            code = getattr(error, "code", None)
            error_code = str(code) if isinstance(code, str) and code else type(error).__name__
            completion = MemberEffectCompletion(
                fence=intent.fence,
                attempt_id=intent.attempt_id,
                action=intent.action,
                succeeded=False,
                error_code=error_code,
                error_detail=str(error)[:300],
            )

        if store is not None:
            try:
                store.complete_cell_member_effect(
                    cell_id=intent.fence.cell_id,
                    epoch=intent.fence.epoch,
                    revision=intent.fence.revision,
                    member=intent.fence.member,
                    effect_name=intent.action.value,
                    correlation_id=correlation_id,
                    attempt_token=intent.attempt_id,
                    succeeded=completion.succeeded,
                    resource_token=completion.reported_digest or correlation_id,
                    error=completion.error_detail,
                    error_code=completion.error_code,
                )
            except Exception:  # noqa: BLE001 - actor completion still settles in-memory fence
                pass

        self._deliver_completion(completion)

    def _execute_intent(self, intent: MemberEffectIntent) -> MemberEffectCompletion:
        harnesses = getattr(self._app, "_harnesses", None)
        if harnesses is None:
            return MemberEffectCompletion(
                fence=intent.fence,
                attempt_id=intent.attempt_id,
                action=intent.action,
                succeeded=False,
                error_code="HARNESS_PORT_UNAVAILABLE",
                error_detail="daemon harness port is unavailable",
            )
        agent_actor = self._agent_actor(intent.agent_id)
        own = self._own_harness(intent)
        if own is not None:
            return self._execute_own(intent, harnesses, own)
        if intent.action is MemberEffectAction.ENSURE:
            return self._execute_ensure(intent, harnesses, agent_actor)
        if intent.action is MemberEffectAction.STOP:
            return self._execute_stop(intent, harnesses, agent_actor)
        return self._execute_policy_apply(intent, harnesses, agent_actor)

    def _execute_ensure(
        self,
        intent: MemberEffectIntent,
        harnesses: Any,
        agent_actor: str,
    ) -> MemberEffectCompletion:
        spec = intent.spec
        if spec is None:
            return self._completion(
                intent, None, "MISSING_DESIRED_SPEC", "ensure intent requires a desired member spec"
            )
        launch_member = spec.launch_member_name
        try:
            member_actor_uri(agent_actor, launch_member)
        except ValueError as error:
            return self._completion(intent, None, HARNESS_START_FAILED, str(error)[:300])
        row = _desired_row(self._app, intent.agent_id, intent.fence.member)
        launch_spec = self._launch_spec(spec, row, agent_actor)
        live_kind = launch_spec.harness
        harness_id = f"{live_kind}:{launch_member}"
        adopted = self._adopt_live(harnesses, harness_id)
        if not adopted and row is not None and row.harness != launch_spec.harness:
            row_harness_id = f"{row.harness}:{launch_member}"
            if self._adopt_live(harnesses, row_harness_id):
                adopted, live_kind, harness_id = True, row.harness, row_harness_id
        key = self._policy_key(spec, agent_actor)
        policies = self._policies()
        if not adopted:
            if policies is not None:
                policies.publish(key, spec.policy, version=intent.fence.revision)
            harnesses.start(launch_spec)
        ready = harnesses.wait_ready(
            live_kind,
            agent_actor,
            harnesses.actor.start_timeout,
            member=launch_member,
        )
        if not ready:
            return self._completion(
                intent, None, "MEMBER_NOT_READY", f"{intent.fence.member} did not report ready"
            )
        live_spec = harnesses.actor.projection.read_launch_spec(harness_id)
        live_status = harnesses.actor.read_harness(harness_id)
        if live_spec is None or live_status is None or not live_status.running:
            return self._completion(
                intent, None, "MEMBER_NOT_RUNNING", f"{harness_id} is not running after start"
            )
        live_policy = spec.policy_digest
        if spec.policy is not None:
            if adopted:
                live_policy = self._ready_ack(key)[0]
            else:
                refusal = self._verify_policy_ack(key, spec)
                if refusal is not None:
                    return self._completion(intent, None, "POLICY_NOT_ACKNOWLEDGED", refusal)
        live_digest = self._digest_of(intent, spec, live_spec, live_policy)
        if adopted and live_digest != spec.config_digest:
            _log(
                self._app,
                "warn",
                "cell.reconcile.member.adopted_with_difference",
                cell_id=intent.fence.cell_id,
                member=intent.fence.member,
                harness_id=harness_id,
                desired_digest=spec.config_digest,
                live_digest=live_digest,
                desired_args=list(spec.args),
                live_args=list(getattr(live_spec, "args", ()) or ()),
            )
        if live_digest == spec.config_digest:
            if adopted and policies is not None:
                policies.publish(key, spec.policy, version=self._ready_ack(key)[1]
                                 or intent.fence.revision)
            if policies is not None:
                policies.mark_verified(key)
            if spec.policy is not None:
                self._policy_event("info", POLICY_APPLIED_EVENT, intent, spec,
                                   action="adopt" if adopted else "ensure")
            if not adopted:
                self._persist_document_fields(intent, row, launch_spec)
        restart_key = (intent.agent_id, intent.fence.member)
        with self._lock:
            restarted_once = restart_key in self._drift_stopped
            if live_digest == spec.config_digest:
                self._drift_stopped.discard(restart_key)
        return MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=intent.action,
            succeeded=True,
            reported_digest=live_digest,
            adopted=adopted and not restarted_once,
        )

    def _launch_spec(self, spec: DesiredMemberSpec, row: Any | None, agent_actor: str) -> Any:
        if row is None:
            return spec.to_launch_spec(agent_actor)
        base = _restore_launch_row(self._app, row)
        if row.harness != spec.harness:
            base = replace(base, session_ref=None)
        return replace(
            base,
            harness=spec.harness,
            model=spec.model,
            cwd=spec.cwd,
            command=spec.command,
            args=spec.args,
            status="running",
        )

    def _persist_document_fields(
        self, intent: MemberEffectIntent, row: Any | None, launch_spec: Any
    ) -> None:
        if row is None or row.harness != launch_spec.harness or row.model == launch_spec.model:
            return
        upsert = getattr(getattr(self._app, "desired_state", None), "upsert_harness", None)
        if not callable(upsert):
            return
        try:
            current = _desired_row(self._app, intent.agent_id, intent.fence.member)
            if current is None or current.harness != row.harness:
                return
            upsert(replace(current, model=launch_spec.model))
        except Exception as error:  # noqa: BLE001 - a lost write is recomputed by adoption drift
            _log(
                self._app,
                "warn",
                "cell.reconcile.member.persist_failed",
                cell_id=intent.fence.cell_id,
                member=intent.fence.member,
                error=str(error)[:300],
            )

    @staticmethod
    def _adopt_live(harnesses: Any, harness_id: str) -> bool:
        status = harnesses.actor.read_harness(harness_id)
        return status is not None and bool(status.running or status.starting)

    def _execute_policy_apply(
        self,
        intent: MemberEffectIntent,
        harnesses: Any,
        agent_actor: str,
    ) -> MemberEffectCompletion:
        spec = intent.spec
        if spec is None:
            return self._completion(
                intent, None, "MISSING_DESIRED_SPEC", "policy apply requires a desired spec"
            )
        key = self._policy_key(spec, agent_actor)
        policies = self._policies()
        if policies is None:
            return self._completion(
                intent, None, "POLICY_LOOKUP_UNAVAILABLE", "no member launch policy lookup"
            )
        previous = intent.previous
        self._policy_event("info", POLICY_APPLY_STARTED_EVENT, intent, spec, previous=previous)
        self._remove_member(intent, harnesses, agent_actor, key)
        policies.publish(key, spec.policy, version=intent.fence.revision)
        digest, code, detail = self._launch_verified(intent, spec, harnesses, agent_actor)
        if code is None:
            self._policy_event(
                "info", POLICY_APPLIED_EVENT, intent, spec, previous=previous, action="apply"
            )
            return self._completion(intent, digest, None, None)
        if previous is None:
            self._policy_event("warn", POLICY_APPLY_FAILED_EVENT, intent, spec,
                               code=code, detail=detail, rolledBack=False)
            return self._completion(intent, None, code, detail)
        self._remove_member(intent, harnesses, agent_actor, key)
        policies.restore_verified(key)
        rolled_back, rollback_code, rollback_detail = self._launch_verified(
            intent, previous, harnesses, agent_actor
        )
        self._policy_event("warn", POLICY_APPLY_FAILED_EVENT, intent, spec, previous=previous,
                           code=code, detail=detail, rolledBack=rollback_code is None,
                           rollbackCode=rollback_code)
        if rollback_code is not None:
            return self._completion(
                intent, None, code, f"{detail}; rollback failed: {rollback_detail}"
            )
        return MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=intent.action,
            succeeded=False,
            reported_digest=rolled_back,
            error_code=code,
            error_detail=detail,
        )

    def _launch_verified(
        self,
        intent: MemberEffectIntent,
        spec: DesiredMemberSpec,
        harnesses: Any,
        agent_actor: str,
    ) -> tuple[str | None, str | None, str | None]:
        row = _desired_row(self._app, intent.agent_id, intent.fence.member)
        launch_spec = self._launch_spec(spec, row, agent_actor)
        launch_member = spec.launch_member_name
        harnesses.start(launch_spec)
        ready = harnesses.wait_ready(
            launch_spec.harness,
            agent_actor,
            harnesses.actor.start_timeout,
            member=launch_member,
        )
        if not ready:
            return None, "MEMBER_NOT_READY", f"{intent.fence.member} did not report ready"
        harness_id = f"{launch_spec.harness}:{launch_member}"
        reported_digest = self._live_digest(intent, spec, harnesses, harness_id)
        if reported_digest is None:
            return None, "MEMBER_NOT_RUNNING", f"{harness_id} is not running after start"
        key = self._policy_key(spec, agent_actor)
        if spec.policy is not None:
            refusal = self._verify_policy_ack(key, spec)
            if refusal is not None:
                return None, "POLICY_NOT_ACKNOWLEDGED", refusal
        policies = self._policies()
        if policies is not None:
            policies.mark_verified(key)
        return reported_digest, None, None

    def _live_digest(
        self, intent: MemberEffectIntent, spec: DesiredMemberSpec, harnesses: Any, harness_id: str
    ) -> str | None:
        live_spec = harnesses.actor.projection.read_launch_spec(harness_id)
        live_status = harnesses.actor.read_harness(harness_id)
        if live_spec is None or live_status is None or not live_status.running:
            return None
        return self._digest_of(intent, spec, live_spec, spec.policy_digest)

    def _digest_of(
        self,
        intent: MemberEffectIntent,
        spec: DesiredMemberSpec,
        live_spec: Any,
        policy_digest: str | None,
    ) -> str:
        return live_member_digest(
            self._app,
            agent_id=intent.agent_id,
            member=intent.fence.member,
            env_ref=spec.env_ref,
            policy_digest=policy_digest,
            live_spec=live_spec,
        )

    def _verify_policy_ack(self, key: PolicyKey, spec: DesiredMemberSpec) -> str | None:
        policies = self._policies()
        assert spec.policy is not None
        if policies is None:
            return "no member launch policy lookup"
        launch = policies.policy_for_key(key)
        if launch is None or launch.digest != spec.policy.digest:
            return "the published launch policy does not match the desired policy"
        try:
            require_policy_ack(
                policies.ready_frame(key), digest=launch.digest, version=launch.version
            )
        except PolicyHandshakeError as error:
            return str(error)
        return None

    def _remove_member(
        self,
        intent: MemberEffectIntent,
        harnesses: Any,
        agent_actor: str,
        key: PolicyKey,
    ) -> None:
        launch_member = self._stop_member(intent)
        for harness_kind in self._stop_targets(intent, harnesses, agent_actor, launch_member):
            harnesses.remove(harness_kind, agent_actor, member=launch_member)
        policies = self._policies()
        if policies is not None:
            policies.discard_process(key)

    def _policy_event(
        self,
        level: str,
        event: str,
        intent: MemberEffectIntent,
        spec: DesiredMemberSpec,
        *,
        previous: DesiredMemberSpec | None = None,
        **fields: Any,
    ) -> None:
        log = getattr(self._app, "_log", None)
        if not callable(log):
            return
        try:
            log(level, "cells", event,
                cell=intent.fence.cell_id, agent=intent.agent_id, member=intent.fence.member,
                harness=spec.harness, revision=intent.fence.revision,
                policyDigest=spec.policy_digest,
                previousPolicyDigest=None if previous is None else previous.policy_digest,
                **fields)
        except Exception:  # noqa: BLE001 - an observability event never changes an outcome
            pass

    @staticmethod
    def _policy_key(spec: DesiredMemberSpec, agent_actor: str) -> PolicyKey:
        return (spec.harness, agent_actor, spec.launch_member_name)

    def _policies(self) -> MemberLaunchPolicies | None:
        policies = getattr(self._app, "_cell_member_policies", None)
        return policies if isinstance(policies, MemberLaunchPolicies) else None

    @staticmethod
    def _completion(
        intent: MemberEffectIntent,
        digest: str | None,
        code: str | None,
        detail: str | None,
    ) -> MemberEffectCompletion:
        return MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=intent.action,
            succeeded=code is None,
            reported_digest=digest if code is None else None,
            error_code=code,
            error_detail=detail,
        )

    def _execute_stop(
        self,
        intent: MemberEffectIntent,
        harnesses: Any,
        agent_actor: str,
    ) -> MemberEffectCompletion:
        launch_member = self._stop_member(intent)
        targets = self._stop_targets(intent, harnesses, agent_actor, launch_member)
        for harness_kind in targets:
            harnesses.remove(harness_kind, agent_actor, member=launch_member)
        policies = self._policies()
        if policies is not None:
            for harness_kind in targets:
                policies.forget((harness_kind, agent_actor, launch_member))
        with self._lock:
            self._started_specs.pop((intent.agent_id, intent.fence.member), None)
            if intent.reason == "adopted-spec-drift":
                self._drift_stopped.add((intent.agent_id, intent.fence.member))
        return MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=intent.action,
            succeeded=True,
        )

    def _stop_targets(
        self,
        intent: MemberEffectIntent,
        harnesses: Any,
        agent_actor: str,
        launch_member: str,
    ) -> tuple[str, ...]:
        found: list[str] = []
        member = intent.fence.member
        with self._lock:
            started = self._started_specs.get((intent.agent_id, member))
        if started is not None and started.harness not in found:
            found.append(started.harness)
        for row in harnesses.actor.read_harnesses():
            if row.harness_id == f"{row.runtime}:{launch_member}" and row.name in {
                agent_actor,
                intent.agent_id,
                launch_member,
            }:
                if row.runtime not in found:
                    found.append(row.runtime)
        if not found:
            facts = self._facts_port.facts_for(intent.agent_id, member)
            if facts is not None and facts.harness and facts.harness not in found:
                found.append(facts.harness)
        return tuple(found)

    def _stop_member(self, intent: MemberEffectIntent) -> str:
        member = intent.fence.member
        if intent.spec is not None:
            return intent.spec.launch_member_name
        with self._lock:
            started = self._started_specs.get((intent.agent_id, member))
        if started is not None:
            return started.launch_member_name
        facts = self._facts_port.facts_for(intent.agent_id, member)
        launch_member = facts.launch_member if facts is not None else None
        return launch_member or member

    def _agent_actor(self, agent_id: str) -> str:
        agent = _agent_for_id(self._app, agent_id)
        actor = getattr(agent, "actor", None) if agent is not None else None
        return str(actor) if isinstance(actor, str) and actor else agent_id

    def _lifecycle_store(self) -> _LifecycleStore | None:
        state_db = getattr(self._app, "state_db", None)
        if state_db is None:
            return None
        with self._lock:
            return _LifecycleStore(state_db)

    def _deliver_completion(self, completion: MemberEffectCompletion) -> None:
        with self._lock:
            if self._closed:
                return
            coordinator = self._coordinator
        if coordinator is not None:
            coordinator.complete_effect(completion)


__all__ = [
    "AppLocalMemberFacts",
    "AppMemberEffectPort",
    "CellReconcileStartBarrier",
    "POLICY_APPLIED_EVENT",
    "POLICY_APPLY_FAILED_EVENT",
    "POLICY_APPLY_STARTED_EVENT",
    "POLICY_UNAVAILABLE_EVENT",
    "launch_member_name",
    "member_launch_policy",
    "nudge_member_reconcile",
]
